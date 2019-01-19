import inspect
import re
import random
import logging
import types
from functools import wraps


import syft
from syft import workers
from syft.workers import BaseWorker
from .tensors import TorchTensor, PointerTensor, LoggingTensor
from .torch_attributes import TorchAttributes
from .tensors.abstract import initialize_tensor


class TorchHook:
    """A Hook which Overrides Methods on PyTorch Tensors.

    The purpose of this class is to:
        * extend torch methods to allow for the moving of tensors from one
        worker to another.
        * override torch methods to execute commands on one worker that are
        called on tensors controlled by the local worker.

    This class is typically the first thing you will initialize when using
    PySyft with PyTorch because it is responsible for augmenting PyTorch with
    PySyft's added functionality (such as remote execution).

    Args:
        local_worker: An optional BaseWorker instance that lets you provide a
            local worker as a parameter which TorchHook will assume to be the
            worker owned by the local machine. If you leave it empty,
            TorchClient will automatically initialize a
            :class:`.workers.VirtualWorker` under the assumption you're looking
            to do local experimentation or development.
        is_client: An optional boolean parameter (default True), indicating
            whether TorchHook is being initialized as an end-user client.This
            can impact whether or not variables are deleted when they fall out
            of scope. If you set this incorrectly on a end user client, Tensors
            and Variables will never be deleted. If you set this incorrectly on
            a remote machine (not a client), tensors will not get saved. It's
            really only important if you're not initializing the local worker
            yourself.
        verbose: An optional boolean parameter (default True) to indicate
            whether or not to print the operations as they occur.
        queue_size: An integer optional parameter (default 0) to specify the
            max length of the list that stores the messages to be sent.

    Example:
        >>> import syft as sy
        >>> hook = sy.TorchHook()
        Hooking into Torch...
        Overloading Complete.
        >>> x = sy.Tensor([-2,-1,0,1,2,3])
        >>> x
        -2
        -1
        0
        1
        2
        3
        [syft.core.frameworks.torch.tensor.FloatTensor of size 6]
    """

    def __init__(
        self, torch, local_worker: BaseWorker = None, is_client: bool = True, verbose: bool = True
    ):
        """Initializes the hook.

        Initialize the hook and define all the attributes pertaining to the
        torch hook in a special TorchAttibute class, that will be added in the
        syft.torch attributes. Hence, this parameters are now conveyed by the
        syft module.
        """
        # Save the provided torch module as an attribute of the hook
        self.torch = torch

        # Save the local worker as an attribute
        self.local_worker = local_worker

        if hasattr(torch, "torch_hooked"):
            logging.warning("Torch was already hooked... skipping hooking process")
            self.local_worker = syft.local_worker
            return
        else:
            torch.torch_hooked = True

        # Add all the torch attributes in the syft.torch attr
        syft.torch = TorchAttributes(torch, self)

        if self.local_worker is None:
            # Every TorchHook instance should have a local worker which is
            # responsible for interfacing with other workers. The worker
            # interface is what allows the Torch specific code in TorchHook to
            # be agnostic to the means by which workers communicate (such as
            # peer-to-peer, sockets, through local ports, or all within the
            # same process)
            self.local_worker = workers.VirtualWorker(
                hook=self, is_client_worker=is_client, id="me"
            )
        else:
            self.local_worker.hook = self

        self.to_auto_overload = {}

        self.args_hook_for_overloaded_attr = {}

        self._hook_native_tensor(torch.Tensor, TorchTensor)

        # add all hooked tensor methods to pointer but change behaviour
        self._hook_pointer_tensor_methods()

        # add all hooked tensor methods to Logging tensor but change behaviour
        self._hook_syft_tensor_methods(LoggingTensor)

        self._hook_torch_module()

        # Add the local_worker to syft so that it can be found if the hook is
        # called several times
        syft.local_worker = self.local_worker

    def _hook_native_tensor(self, tensor_type: type, syft_type: type):
        """Adds PySyft Tensor Functionality to the given native tensor type.

        Overloads the given native Torch tensor to add PySyft Tensor
        Functionality. Overloading involves modifying the tensor type with
        PySyft's added functionality. You may read about what kind of
        modifications are made in the methods that this method calls.

        Args:
            tensor_type: The type of tensor being hooked (in this refactor
                this is only ever torch.Tensor, but in previous versions of
                PySyft this iterated over all tensor types.
            syft_type: The abstract type whose methods should all be added to
                the tensor_type class. In practice this is always TorchTensor.
                Read more about it there.
        """
        # Reinitialize init method of Torch tensor with Syft init
        self._add_registration_to___init__(tensor_type, torch_tensor=True)

        # Overload Torch tensor properties with Syft properties
        self._hook_properties(tensor_type)

        # Returns a list of methods to be overloaded, stored in the dict to_auto_overload
        # with tensor_type as a key
        self.to_auto_overload[tensor_type] = self._which_methods_should_we_auto_overload(
            tensor_type
        )

        # [We don't rename native methods as torch tensors are not hooked] Rename native functions
        # #self._rename_native_functions(tensor_type)

        # Overload auto overloaded with Torch methods
        self._add_methods_from__torch_tensor(tensor_type, syft_type)

        self._hook_native_methods(tensor_type)

    def _hook_native_methods(self, tensor_type: type):
        """
        Add hooked version of all methods of the tensor_type to the
        Pointer tensor: instead of performing the native tensor
        method, it will be sent remotely to the location the pointer
        is pointing at.
        :param tensor_type: the tensor_type which holds the methods
        """
        # # Add methods defined in the TorchTensor class to the Pointer class
        # self._add_methods_from__torch_tensor(PointerTensor, TorchTensor)

        # Use a pre-defined list to select the methods to overload
        for attr in self.to_auto_overload[tensor_type]:
            # if we haven't already overloaded this function
            if f"native_{attr}" not in dir(tensor_type):
                native_method = getattr(tensor_type, attr)
                setattr(tensor_type, f"native_{attr}", native_method)
                new_method = self.get_hooked_method(attr)
                setattr(tensor_type, attr, new_method)

    def _hook_syft_tensor_methods(self, syft_type: type):
        """
        Add hooked version of all methods of the tensor_type to the
        Pointer tensor: instead of performing the native tensor
        method, it will be sent remotely to the location the pointer
        is pointing at.
        :param tensor_type: the tensor_type which holds the methods
        """

        tensor_type = self.torch.Tensor
        # Use a pre-defined list to select the methods to overload
        for attr in self.to_auto_overload[tensor_type]:
            if attr not in dir(syft_type):  # TODO and add special functions to include / avoid
                new_method = self.get_hooked_syft_method(attr)
                setattr(syft_type, attr, new_method)

    def _hook_pointer_tensor_methods(self):
        """
        Add hooked version of all methods of the tensor_type to the
        Pointer tensor: instead of performing the native tensor
        method, it will be sent remotely to the location the pointer
        is pointing at.
        """

        tensor_type = self.torch.Tensor
        # Use a pre-defined list to select the methods to overload
        for attr in self.to_auto_overload[tensor_type]:
            if attr not in dir(PointerTensor):  # TODO and add special functions to include / avoid
                new_method = self.get_hooked_pointer_method(attr)
                setattr(PointerTensor, attr, new_method)

    def _hook_torch_module(self):
        """Overloads functions in the main torch modules.
        The way this is accomplished is by first moving all existing module
        functions in the torch module to native_<function_name_here>.

        Example:
            the real :func:`torch.cat` will become :func:`torch.native_cat`
            and :func:`torch.cat` will have our hooking code.
        """

        torch_modules = {"torch.nn.functional": self.torch.nn.functional}
        # TODO Replace with syft.torch.torch_modules when hooking 'torch' will not break msgpack

        for module_name, torch_module in torch_modules.items():
            for func in dir(torch_module):
                # Some functions we want to ignore (not override). Such functions have been hard
                # coded into the torch_attribute exclude (see TorchAttribute class)
                if func in syft.torch.exclude:
                    continue

                # If we haven't already overloaded this function
                if "native_" in func or f"native_{func}" in dir(torch_module):
                    continue

                # Where the overloading happens
                # 1. Get native function
                native_func = getattr(torch_module, func)
                # 2. Check it is a proper function
                if type(native_func) in [types.FunctionType, types.BuiltinFunctionType]:
                    # 3. Build the hooked function
                    new_func = self.get_hooked_func(native_func)
                    # 4. Move the native function
                    setattr(torch_module, f"native_{func}", native_func)
                    # 5. Put instead the hooked one
                    setattr(torch_module, func, new_func)

    def get_hooked_pointer_method(hook_self, attr):
        """
        Hook a method to send it to remote worker
        :param attr: the method to hook
        :return: the hooked method
        """

        @wraps(attr)
        def overloaded_attr(self, *args, **kwargs):
            """
            Operate the hooking
            """
            pointer = self
            # Get info on who needs to send where the command
            owner = pointer.owner
            location = pointer.location

            # Send the command
            command = (attr, self, args)
            response = owner.send_command(location, command)
            return response

        return overloaded_attr

    def get_hooked_syft_method(hook_self, attr):
        """
        Hook a method in order to replace all args/kwargs syft/torch tensors with
        their child attribute, forward this method with the new args and new self,
        get response and "rebuild" the syft tensor wrapper upon all tensors found
        :param attr: the method to hook
        :return: the hooked method
        """

        @wraps(attr)
        def overloaded_attr(self, *args, **kwargs):
            """
            Operate the hooking
            """
            # TODO: I can't manage the import issue, can you?
            # Replace all syft tensor with their child attribute
            new_self, new_args = syft.frameworks.torch.hook_args.hook_method_args(attr, self, args)

            # Send it to the appropriate class and get the response
            response = getattr(new_self, attr)(*new_args, **kwargs)

            # Put back SyftTensor on the tensors found in the response
            response = syft.frameworks.torch.hook_args.hook_response(
                attr, response, wrap_type=type(self)
            )
            return response

        return overloaded_attr

    def get_hooked_method(hook_self, attr):
        """
        Hook a method in order to replace all args/kwargs syft/torch tensors with
        their child attribute if they exist
        If so, forward this method with the new args and new self, get response
        and "rebuild" the torch tensor wrapper upon all tensors found
        If not, just execute the native torch method
        :param attr: the method to hook
        :return: the hooked method
        """

        @wraps(attr)
        def overloaded_attr(self, *args, **kwargs):
            """
            Operate the hooking
            """
            if not hasattr(self, "child"):  # means that it's not a wrapper
                cmd = getattr(self, f"native_{attr}")
                # Run the native function with the new args
                if isinstance(args, tuple):
                    response = cmd(*args)
                else:
                    response = cmd(args)
            else:  # means that there is a wrapper to remove
                # Replace all torch tensor with their child attribute
                new_self, new_args = syft.frameworks.torch.hook_args.hook_method_args(
                    attr, self, args
                )

                # Send the new command to the appropriate class and get the response
                cmd = getattr(new_self, attr)
                response = cmd(*new_args, **kwargs)
                # Put back the wrappers where needed
                response = syft.frameworks.torch.hook_args.hook_response(
                    attr, response, wrap_type=type(self)
                )

            return response

        return overloaded_attr

    def get_hooked_func(hook_self, attr):
        """
        Hook a function in order to inspect its args and search for pointer
        or other syft tensors.
        - Calls to this function with normal tensors or numbers / string trigger
          usual behaviour
        - Calls with pointers send the command to the location of the pointer(s)
        - Calls with syft tensor will in the future trigger specific behaviour

        :param attr: the function to hook
        :return: the hooked function
        """

        @wraps(attr)
        def overloaded_attr(*args, **kwargs):
            """
            Operate the hooking
            """
            cmd_name = f"{attr.__module__}.{attr.__name__}"
            command = (cmd_name, None, args)  # TODO add kwargs
            response = TorchTensor.handle_func_command(command)
            return response

        return overloaded_attr

    def _add_registration_to___init__(hook_self, tensor_type: type, torch_tensor: bool = False):
        """Adds several attributes to the tensor.

        Overloads tensor_type.__init__ to add several attributes to the tensor
        as well as (optionally) registering the tensor automatically.
        TODO: auto-registration is disabled at the moment, this might be bad.

        Args:
            tensor_type: The type of tensor being hooked (in this refactor this
                is only ever torch.Tensor, but in previous versions of PySyft
                this iterated over all tensor types.
            torch_tensor: An optional boolean parameter (default False) to
                specify whether to skip running the native initialization
                logic. TODO: this flag might never get used.
        """
        if "native___init__" not in dir(tensor_type):
            tensor_type.native___init__ = tensor_type.__init__

        def new___init__(cls, *args, owner=None, id=None, register=True, **kwargs):

            initialize_tensor(
                hook_self=hook_self,
                cls=cls,
                id=id,
                torch_tensor=torch_tensor,
                init_args=args,
                init_kwargs=kwargs,
            )

            # if register:
            #     owner.register_object(cls, id=id)

        tensor_type.__init__ = new___init__

    @staticmethod
    def _hook_properties(tensor_type: type):
        """Overloads tensor_type properties.

        This method gets called only on torch.Tensor. If you're not sure how
        properties work, read:
        https://www.programiz.com/python-programming/property

        Args:
            tensor_type: The tensor type which is having properties
                added to it, typically just torch.Tensor.
        """

        @property
        def location(self):
            return self.child.location

        tensor_type.location = location

        @property
        def id_at_location(self):
            return self.child.id_at_location

        tensor_type.id_at_location = id_at_location

        @property
        def id(self):
            if not hasattr(self, "_id"):
                self._id = int(10e10 * random.random())
            return self._id

        @id.setter
        def id(self, new_id):
            self._id = new_id
            return self

        tensor_type.id = id

        @property
        def is_wrapper(self):
            if not hasattr(self, "_is_wrapper"):
                self._is_wrapper = False
            return self._is_wrapper

        @is_wrapper.setter
        def is_wrapper(self, it_is_a_wrapper):
            self._is_wrapper = it_is_a_wrapper
            return self

        tensor_type.is_wrapper = is_wrapper

    def _which_methods_should_we_auto_overload(self, tensor_type: type):
        """Creates a list of Torch methods to auto overload.

        By default, it looks for the intersection between the methods of
        tensor_type and torch_type minus those in the exception list
        (syft.torch.exclude).

        Args:
            tensor_type: Iterate through the properties of this tensor type.
            syft_type: Iterate through all attributes in this type.

        Returns:
            A list of methods to be overloaded.
        """

        to_overload = []

        for attr in dir(tensor_type):

            # Conditions for overloading the method
            if attr in syft.torch.exclude:
                continue
            if not hasattr(tensor_type, attr):
                continue

            lit = getattr(tensor_type, attr)
            is_base = attr in dir(object)
            is_desc = inspect.ismethoddescriptor(lit)
            is_func = isinstance(lit, types.FunctionType)
            try:
                is_service_func = "HookService" in lit.__qualname__
            except AttributeError:
                is_service_func = False
            is_overloaded = re.match("native*", attr) is not None

            if (is_desc or (is_func and not is_service_func)) and not is_base and not is_overloaded:
                to_overload.append(attr)

        return to_overload

    @staticmethod
    def _add_methods_from__torch_tensor(tensor_type: type, syft_type: type):
        """Adds methods from the TorchTensor class to the native torch tensor.

        The class TorchTensor is a proxy to avoid extending directly the torch
        tensor class.

        Args:
            tensor_type: The tensor type to which we are adding methods
                from TorchTensor class.
        """
        exclude = [
            "__class__",
            "__delattr__",
            "__dir__",
            "__doc__",
            "__dict__",
            "__format__",
            "__getattribute__",
            "__hash__",
            "__init__",
            "__init_subclass__",
            "__weakref__",
            "__ne__",
            "__new__",
            "__reduce__",
            "__reduce_ex__",
            "__setattr__",
            "__sizeof__",
            "__subclasshook__",
            "_get_type",
            "__eq__",
            "__gt__",
            "__ge__",
            "__lt__",
            "__le__",
        ]
        # For all methods defined in TorchTensor which are not internal methods (like __class__etc)
        for attr in dir(syft_type):
            if attr not in exclude:
                if hasattr(tensor_type, attr):
                    setattr(tensor_type, f"native_{attr}", getattr(tensor_type, attr))
                # Add to the native tensor this method
                setattr(tensor_type, attr, getattr(TorchTensor, attr))
