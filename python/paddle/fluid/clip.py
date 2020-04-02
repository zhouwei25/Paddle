#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import copy
import six
import warnings

import functools
from . import layers
from . import framework
from . import core
from . import name_scope
from .dygraph import base as imperative_base

__all__ = [
    'set_gradient_clip', 'ErrorClipByValue', 'GradientClipByValue',
    'GradientClipByNorm', 'GradientClipByGlobalNorm'
]


class BaseErrorClipAttr(object):
    def __str__(self):
        raise NotImplementedError()

    def _append_clip_op(self, block, grad_name):
        raise NotImplementedError()


class ErrorClipByValue(BaseErrorClipAttr):
    """
    Clips tensor values to the range [min, max].

    Given a tensor ``t`` (see Examples below), this operation clips its value \
    to ``min`` and ``max`` inplace.

    - Any values less than min are set to min.
    - Any values greater than max are set to max.

    Args:
        max (float): The maximum value to clip by.
        min (float, optional): The minimum value to clip by. if not set by user, \
        will be set to ``-max`` by framework.

    Examples:
        .. code-block:: python

            import paddle.fluid as fluid
            BATCH_SIZE = 128
            CLIP_MAX = 2e-6
            CLIP_MIN = -1e-6
            prog = fluid.framework.Program()
            with fluid.program_guard(main_program=prog):
                image = fluid.layers.data(
                    name='x', shape=[784], dtype='float32')
                hidden1 = fluid.layers.fc(input=image, size=128, act='relu')
                hidden2 = fluid.layers.fc(input=hidden1, size=64, act='relu')
                predict = fluid.layers.fc(
                    input=hidden2, size=10, act='softmax')
                label = fluid.layers.data(name='y', shape=[1], dtype='int64')
                cost = fluid.layers.cross_entropy(input=predict, label=label)
                avg_cost = fluid.layers.mean(cost)
            prog_clip = prog.clone()
            prog_clip.block(0).var(hidden1.name)._set_error_clip(
                fluid.clip.ErrorClipByValue(
                    max=CLIP_MAX, min=CLIP_MIN)
    """

    def __init__(self, max, min=None):
        max = float(max)
        if min is None:
            min = -max
        else:
            min = float(min)
        self.max = max
        self.min = min

    def __str__(self):
        return "ByValue, min=%f, max=%f" % (self.min, self.max)

    def _append_clip_op(self, block, grad_name):
        clip_op_desc = block.desc.append_op()
        clip_op_desc.set_type("clip")
        clip_op_desc.set_input("X", [grad_name])
        clip_op_desc.set_output("Out", [grad_name])
        clip_op_desc._set_attr("min", self.min)
        clip_op_desc._set_attr("max", self.max)


def error_clip_callback(block, context):
    # the context is a grad_to_var map
    grad_to_var = context
    op_desc = block.desc.op(block.desc.op_size() - 1)
    for grad_n in [n for n in op_desc.output_arg_names() if n in grad_to_var]:
        fwd_var = block._var_recursive(grad_to_var[grad_n])
        error_clip = getattr(fwd_var, "error_clip", None)
        if not (error_clip is None or isinstance(error_clip,
                                                 BaseErrorClipAttr)):
            raise TypeError(
                "Variable's error_clip should be an instance of BaseErrorClipAttr or None."
            )
        if error_clip is not None:
            error_clip._append_clip_op(block, grad_n)


class GradientClipBase(object):
    def __init__(self, need_clip=None):
        if need_clip is not None and not callable(need_clip):
            raise TypeError(
                "The type of need_clip must be funciton, and it can filter out "
                "parameter that does't need gradient clip. This function must return "
                "True or False, and True means that clipping is required. Please refer to "
                "API documention of GradientClipByGlobalNorm / GradientClipByNorm "
                "/GradientClipByValue.")
        self._need_clip_func = need_clip

    def __str__(self):
        raise NotImplementedError()

    @imperative_base.no_grad
    def _dygraph_clip(self, params_grads):
        raise NotImplementedError

    def _static_clip(self, params_grads):
        raise NotImplementedError

    def __call__(self, params_grads):
        assert len(
            params_grads
        ) > 0, "The number of trainable parameters should be greater than 0."
        if framework.in_dygraph_mode():
            return self._dygraph_clip(params_grads)
        else:
            for p, g in params_grads:
                if getattr(p, 'gradient_clip_attr', None) is not None:
                    warnings.warn(
                        "'set_gradient_clip' will be ineffective, because you have "
                        "pass 'grad_clip' into 'minimize'. So, 'set_gradient_clip' "
                        "is redundant and you can remove it.")
                    break
            return self._static_clip(params_grads)

    def _process_context(self, context, param, grad):
        raise NotImplementedError()

    def _create_operators(self, param, grad):
        raise NotImplementedError()


class GradientClipByValue(GradientClipBase):
    """
    Limit the value of multi-dimensional Tensor :math:`X` to the range [min, max].
    
    - Any values less than min are set to ``min``.
    
    - Any values greater than max are set to ``max``.

    The multidimensional Tensor :math:`X` is not passed from this class, but the gradients of all parameters in ``Program`` . If ``need_clip``
    is not None, then only part of gradients can be selected for gradient clipping by ``need_clip`` .
    
    Gradient clip will takes effect after being set in ``optimizer.minimize(grad_clip)`` , see the document ``optimizer`` 
    (for example: :ref:`api_fluid_optimizer_SGDOptimizer`).
    
    Args:
        max (float): The maximum value to clip by.
        min (float, optional): The minimum value to clip by. if not set by user, \
            will be set to ``-max`` automatically. In this case, ``max`` must be greater than 0.
        need_clip (function, optional): Type: function. This function accepts a ``Parameter`` and returns ``bool`` \
            (True: the gradient of this ``Parameter`` need to be clipped, False: not need). \
            Default: None, and all gradients of parameters in the network will be clipped.


    Examples 1:
        .. code-block:: python
            # use for Static mode
            import paddle
            import paddle.fluid as fluid
            import numpy as np
                        
            main_prog = fluid.Program()
            startup_prog = fluid.Program()
            with fluid.program_guard(
                    main_program=main_prog, startup_program=startup_prog):
                image = fluid.data(
                    name='x', shape=[-1, 2], dtype='float32')
                predict = fluid.layers.fc(input=image, size=3, act='relu') # Trainable parameters: fc_0.w.0, fc_0.b.0
                loss = fluid.layers.mean(predict)
                
                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByValue(min=-1, max=1)
                
                # Clip a part of parameters in network: (e.g. fc_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a Parameter, and return bool
                # def fileter_func(Parameter):
                # # It can be easily filtered by Parameter.name (name can be set in fluid.ParamAttr, and the default name is fc_0.w_0, fc_0.b_0)
                #   return Parameter.name=="fc_0.w_0"
                # clip = fluid.clip.GradientClipByValue(min=-1, max=1, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGDOptimizer(learning_rate=0.1)
                sgd_optimizer.minimize(loss, grad_clip=clip)

            place = fluid.CPUPlace()
            exe = fluid.Executor(place)
            x = np.random.uniform(-100, 100, (10, 2)).astype('float32')
            exe.run(startup_prog)
            out = exe.run(main_prog, feed={'x': x}, fetch_list=loss)
        
    Examples 2:
        .. code-block:: python
            # use for Dygraph mode
            import paddle
            import paddle.fluid as fluid
            
            with fluid.dygraph.guard():
                linear = fluid.dygraph.Linear(10, 10)  # Trainable parameters:: linear_0.w.0, linear_0.b.0
                inputs = fluid.layers.uniform_random([32, 10]).astype('float32')
                out = linear(fluid.dygraph.to_variable(inputs))
                loss = fluid.layers.reduce_mean(out)
                loss.backward()

                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByValue(min=-1, max=1)

                # Clip a part of parameters in network: (e.g. linear_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a ParamBase, and return bool
                # def fileter_func(ParamBase):
                # # It can be easily filtered by ParamBase.name(name can be set in fluid.ParamAttr, and the default name is linear_0.w_0, linear_0.b_0)
                #   return ParamBase.name == "linear_0.w_0"
                # # Note: linear.weight and linear.bias can return the weight and bias of dygraph.Linear, respectively, and can be used to filter
                #   return ParamBase.name == linear.weight.name
                # clip = fluid.clip.GradientClipByValue(min=-1, max=1, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGD(
                    learning_rate=0.1, parameter_list=linear.parameters())
                sgd_optimizer.minimize(loss, grad_clip=clip)
    """

    def __init__(self, max, min=None, need_clip=None):
        super(GradientClipByValue, self).__init__(need_clip)
        if min is None:
            assert (max > 0.0)
            min = -max
        self.max = float(max)
        self.min = float(min)

    def __str__(self):
        return "Gradient Clip By Value, min = %f, max=%f" % (self.min, self.max)

    @imperative_base.no_grad
    def _dygraph_clip(self, params_grads):
        params_and_grads = []
        for p, g in params_grads:
            if g is None:
                continue
            if self._need_clip_func is not None and not self._need_clip_func(p):
                params_and_grads.append((p, g))
                continue
            new_grad = layers.clip(x=g, min=self.min, max=self.max)
            params_and_grads.append((p, new_grad))
        return params_and_grads

    def _static_clip(self, params_grads):
        params_and_grads = []
        with framework.name_scope('gradient_clip'):
            for p, g in params_grads:
                if g is None:
                    continue
                if self._need_clip_func is not None and not self._need_clip_func(
                        p):
                    params_and_grads.append((p, g))
                    continue

                with p.block.program._optimized_guard([p, g]):
                    new_grad = layers.clip(x=g, min=self.min, max=self.max)
                params_and_grads.append((p, new_grad))
        _correct_clip_op_role_var(params_and_grads)
        return params_and_grads

    def _process_context(self, context, param, grad):
        pass

    def _create_operators(self, param, grad):
        new_grad = layers.clip(x=grad, min=self.min, max=self.max)
        return param, new_grad


class GradientClipByNorm(GradientClipBase):
    """
    Limit the l2 norm of multi-dimensional Tensor :math:`X` to ``clip_norm`` .
    
    - If the l2 norm of :math:`X` is greater than ``clip_norm`` , :math:`X` will be compressed through a ratio.
    
    - If the l2 norm of :math:`X` is less than or equal to ``clip_norm`` , nothing will be done.
    
    The multidimensional Tensor :math:`X` is not passed from this class, but the gradients of all parameters in ``Program`` . If ``need_clip``
    is not None, then only part of gradients can be selected for gradient clipping by ``need_clip`` .
    
    Gradient clip will takes effect after being set in ``optimizer.minimize(grad_clip)`` , see the document ``optimizer`` 
    (for example: :ref:`api_fluid_optimizer_SGDOptimizer`).
    
    The clipping formula is:

    .. math::
        Out =
        \\left \{
        \\begin{aligned}
        & X & & if (norm(X) \\leq clip\_norm) \\\\
        & \\frac{clip\_norm*X}{norm(X)} & & if (norm(X) > clip\_norm) \\\\
        \\end{aligned}
        \\right.


    where :math:`norm(X)` represents the L2 norm of :math:`X`.

    .. math::
        norm(X) = ( \\sum_{i=1}^{n}|x\_i|^2)^{ \\frac{1}{2}}

    Args:
        clip_norm(float): The maximum norm value.
        need_clip (function, optional): Type: function. This function accepts a ``Parameter`` and returns ``bool``  \
            (True: the gradient of this ``Parameter`` need to be clipped, False: not need).  \
            Default: None, and all gradients of parameters in the network will be clipped.

    Examples 1:
        .. code-block:: python
            # use for Static mode
            import paddle
            import paddle.fluid as fluid
            import numpy as np
                        
            main_prog = fluid.Program()
            startup_prog = fluid.Program()
            with fluid.program_guard(
                    main_program=main_prog, startup_program=startup_prog):
                image = fluid.data(
                    name='x', shape=[-1, 2], dtype='float32')
                predict = fluid.layers.fc(input=image, size=3, act='relu') # Trainable parameters: fc_0.w.0, fc_0.b.0
                loss = fluid.layers.mean(predict)
                
                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByNorm(clip_norm=1.0)
                
                # Clip a part of parameters in network: (e.g. linear_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a Parameter, and return bool
                # def fileter_func(Parameter):
                # # It can be easily filtered by Parameter.name (name can be set in fluid.ParamAttr, and the default name is fc_0.w_0, fc_0.b_0)
                #   return Parameter.name=="fc_0.w_0"
                # clip = fluid.clip.GradientClipByNorm(clip_norm=1.0, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGDOptimizer(learning_rate=0.1)
                sgd_optimizer.minimize(loss, grad_clip=clip)

            place = fluid.CPUPlace()
            exe = fluid.Executor(place)
            x = np.random.uniform(-100, 100, (10, 2)).astype('float32')
            exe.run(startup_prog)
            out = exe.run(main_prog, feed={'x': x}, fetch_list=loss)
            
    Examples 2:
        .. code-block:: python
            # use for Dygraph mode
            import paddle
            import paddle.fluid as fluid
            
            with fluid.dygraph.guard():
                linear = fluid.dygraph.Linear(10, 10)  # Trainable: linear_0.w.0, linear_0.b.0
                inputs = fluid.layers.uniform_random([32, 10]).astype('float32')
                out = linear(fluid.dygraph.to_variable(inputs))
                loss = fluid.layers.reduce_mean(out)
                loss.backward()

                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByNorm(clip_norm=1.0)

                # Clip a part of parameters in network: (e.g. linear_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a ParamBase, and return bool
                # def fileter_func(ParamBase):
                # # It can be easily filtered by ParamBase.name(name can be set in fluid.ParamAttr, and the default name is linear_0.w_0, linear_0.b_0)
                #   return ParamBase.name == "linear_0.w_0"
                # # Note: linear.weight and linear.bias can return the weight and bias of dygraph.Linear, respectively, and can be used to filter
                #   return ParamBase.name == linear.weight.name
                # clip = fluid.clip.GradientClipByNorm(clip_norm=1.0, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGD(
                    learning_rate=0.1, parameter_list=linear.parameters())
                sgd_optimizer.minimize(loss, grad_clip=clip)

    """

    def __init__(self, clip_norm, need_clip=None):
        super(GradientClipByNorm, self).__init__(need_clip)
        self.clip_norm = float(clip_norm)

    def __str__(self):
        return "Gradient Clip By Norm, clip_norm=%f" % self.clip_norm

    @imperative_base.no_grad
    def _dygraph_clip(self, params_grads):
        params_and_grads = []
        for p, g in params_grads:
            if g is None:
                continue
            if self._need_clip_func is not None and not self._need_clip_func(p):
                params_and_grads.append((p, g))
                continue
            new_grad = layers.clip_by_norm(x=g, max_norm=self.clip_norm)
            params_and_grads.append((p, new_grad))
        return params_and_grads

    def _static_clip(self, params_grads):
        params_and_grads = []
        with framework.name_scope('gradient_clip'):
            for p, g in params_grads:
                if g is None:
                    continue
                if self._need_clip_func is not None and not self._need_clip_func(
                        p):
                    params_and_grads.append((p, g))
                    continue

                with p.block.program._optimized_guard([p, g]):
                    new_grad = layers.clip_by_norm(x=g, max_norm=self.clip_norm)
                params_and_grads.append((p, new_grad))
        _correct_clip_op_role_var(params_and_grads)
        return params_and_grads

    def _process_context(self, context, param, grad):
        pass

    def _create_operators(self, param, grad):
        new_grad = layers.clip_by_norm(x=grad, max_norm=self.clip_norm)
        return param, new_grad


class GradientClipByGlobalNorm(GradientClipBase):
    """
    Given a list of Tensor :math:`t_list` , limit the global norm for the elements of all tensors in 
    :math:`t_list` to ``clip_norm`` .
    
    - If the global norm is greater than ``clip_norm`` , all the elements of all tensors will be compressed through a ratio.
    
    - If the global norm is less than or equal to ``clip_norm`` , nothing will be done.
    
    The list of Tensor :math:`t_list` is not passed from this class, but the gradients of all parameters in ``Program`` . If ``need_clip``
    is not None, then only part of gradients can be selected for gradient clipping by ``need_clip`` .
    
    Gradient clip will takes effect after being set in ``optimizer.minimize(grad_clip)`` , see the document ``optimizer`` 
    (for example: :ref:`api_fluid_optimizer_SGDOptimizer`).

    The clipping formula is:

    .. math::

        t\_list[i] = t\_list[i] * \\frac{clip\_norm}{\max(global\_norm, clip\_norm)}

    where:

    .. math::

        global\_norm = \sqrt{\sum_{i=0}^{N-1}(l2norm(t\_list[i]))^2}

    Args:
        clip_norm (float): The maximum norm value.
        group_name (str, optional): The group name for this clip. Default value is 'default_group'
        need_clip (function, optional): Type: function. This function accepts a ``Parameter`` and returns ``bool``  \
            (True: the gradient of this ``Parameter`` need to be clipped, False: not need).  \
            Default: None, and all gradients of parameters in the network will be clipped.

    Examples 1:
        .. code-block:: python
            # use for Static mode
            import paddle
            import paddle.fluid as fluid
            import numpy as np
                        
            main_prog = fluid.Program()
            startup_prog = fluid.Program()
            with fluid.program_guard(
                    main_program=main_prog, startup_program=startup_prog):
                image = fluid.data(
                    name='x', shape=[-1, 2], dtype='float32')
                predict = fluid.layers.fc(input=image, size=3, act='relu') # Trainable parameters: fc_0.w.0, fc_0.b.0
                loss = fluid.layers.mean(predict)
                
                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByGlobalNorm(clip_norm=1.0)
                
                # Clip a part of parameters in network: (e.g. fc_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a ParamBase, and return bool
                # def fileter_func(Parameter):
                # # It can be easily filtered by Parameter.name (name can be set in fluid.ParamAttr, and the default name is fc_0.w_0, fc_0.b_0)
                #   return Parameter.name=="fc_0.w_0"
                # clip = fluid.clip.GradientClipByGlobalNorm(clip_norm=1.0, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGDOptimizer(learning_rate=0.1)
                sgd_optimizer.minimize(loss, grad_clip=clip)

            place = fluid.CPUPlace()
            exe = fluid.Executor(place)
            x = np.random.uniform(-100, 100, (10, 2)).astype('float32')
            exe.run(startup_prog)
            out = exe.run(main_prog, feed={'x': x}, fetch_list=loss)
    
    Examples 2:
        .. code-block:: python
            # use for Dygraph mode
            import paddle
            import paddle.fluid as fluid

            with fluid.dygraph.guard():
                linear = fluid.dygraph.Linear(10, 10)  # Trainable: linear_0.w.0, linear_0.b.0
                inputs = fluid.layers.uniform_random([32, 10]).astype('float32')
                out = linear(fluid.dygraph.to_variable(inputs))
                loss = fluid.layers.reduce_mean(out)
                loss.backward()

                # Clip all parameters in network:
                clip = fluid.clip.GradientClipByGlobalNorm(clip_norm=1.0)

                # Clip a part of parameters in network: (e.g. linear_0.w_0)
                # pass a function(fileter_func) to need_clip, and fileter_func receive a ParamBase, and return bool
                # def fileter_func(ParamBase):
                # # It can be easily filtered by ParamBase.name(name can be set in fluid.ParamAttr, and the default name is linear_0.w_0, linear_0.b_0)
                #   return ParamBase.name == "linear_0.w_0"
                # # Note: linear.weight and linear.bias can return the weight and bias of dygraph.Linear, respectively, and can be used to filter
                #   return ParamBase.name == linear.weight.name
                # clip = fluid.clip.GradientClipByGlobalNorm(clip_norm=1.0, need_clip=fileter_func)

                sgd_optimizer = fluid.optimizer.SGD(
                    learning_rate=0.1, parameter_list=linear.parameters())
                sgd_optimizer.minimize(loss, grad_clip=clip)

    """

    def __init__(self, clip_norm, group_name="default_group", need_clip=None):
        super(GradientClipByGlobalNorm, self).__init__(need_clip)
        self.clip_norm = float(clip_norm)
        self.group_name = group_name

    def __str__(self):
        return "Gradient Clip By GlobalNorm, global_norm=%f" % (self.clip_norm)

    @imperative_base.no_grad
    def _dygraph_clip(self, params_grads):
        params_and_grads = []
        sum_square_list = []
        for p, g in params_grads:
            if g is None:
                continue
            if self._need_clip_func is not None and not self._need_clip_func(p):
                continue
            merge_grad = g
            if g.type == core.VarDesc.VarType.SELECTED_ROWS:
                merge_grad = layers.merge_selected_rows(g)
                merge_grad = layers.get_tensor_from_selected_rows(merge_grad)
            square = layers.square(merge_grad)
            sum_square = layers.reduce_sum(square)
            sum_square_list.append(sum_square)

        # all parameters have been filterd out
        if len(sum_square_list) == 0:
            return params_grads

        global_norm_var = layers.concat(sum_square_list)
        global_norm_var = layers.reduce_sum(global_norm_var)
        global_norm_var = layers.sqrt(global_norm_var)
        max_global_norm = layers.fill_constant(
            shape=[1], dtype='float32', value=self.clip_norm)
        clip_var = layers.elementwise_div(
            x=max_global_norm,
            y=layers.elementwise_max(
                x=global_norm_var, y=max_global_norm))
        for p, g in params_grads:
            if g is None:
                continue
            if self._need_clip_func is not None and not self._need_clip_func(p):
                params_and_grads.append((p, g))
                continue
            new_grad = layers.elementwise_mul(x=g, y=clip_var)
            params_and_grads.append((p, new_grad))

        return params_and_grads

    def _static_clip(self, params_grads):
        params_and_grads = []
        sum_square_list = []
        with framework.name_scope('gradient_clip'):
            for p, g in params_grads:
                if g is None:
                    continue
                if self._need_clip_func is not None and not self._need_clip_func(
                        p):
                    continue
                merge_grad = g
                with p.block.program._optimized_guard([p, g]):
                    if g.type == core.VarDesc.VarType.SELECTED_ROWS:
                        merge_grad = layers.merge_selected_rows(g)
                        merge_grad = layers.get_tensor_from_selected_rows(
                            merge_grad)

                    square = layers.square(merge_grad)
                    sum_square = layers.reduce_sum(input=square)
                    sum_square_list.append(sum_square)

            # all parameters have been filterd out
            if len(sum_square_list) == 0:
                return params_grads

            with p.block.program._optimized_guard([p, g]):
                global_norm_var = layers.sums(sum_square_list)
                global_norm_var = layers.sqrt(x=global_norm_var)
                max_global_norm = layers.fill_constant(
                    shape=[1], dtype="float32", value=self.clip_norm)
                scale_var = layers.elementwise_div(
                    x=max_global_norm,
                    y=layers.elementwise_max(
                        x=max_global_norm, y=global_norm_var))

            for p, g in params_grads:
                if g is None:
                    continue
                if self._need_clip_func is not None and not self._need_clip_func(
                        p):
                    params_and_grads.append((p, g))
                    continue

                with p.block.program._optimized_guard([p, g]):
                    new_grad = layers.elementwise_mul(x=g, y=scale_var)
                params_and_grads.append((p, new_grad))

        _correct_clip_op_role_var(params_and_grads)
        return params_and_grads

    def _process_context(self, context, param, grad):
        if self.group_name not in context:
            context[self.group_name] = []
            context[self.group_name + "_clip_value"] = self.clip_norm
            context[self.group_name + "_clip"] = layers.fill_constant(
                shape=[1], dtype="float32", value=self.clip_norm)
        else:
            if not self.clip_norm == context[self.group_name + "_clip_value"]:
                raise ValueError(
                    "All parameters' 'clip_norm' of a same group should be the same"
                )

        merge_grad = grad
        if grad.type == core.VarDesc.VarType.SELECTED_ROWS:
            merge_grad = layers.merge_selected_rows(grad)
            merge_grad = layers.get_tensor_from_selected_rows(merge_grad)

        square = layers.square(merge_grad)
        local_norm_var = layers.reduce_sum(input=square)
        context[self.group_name].append(local_norm_var)

        self.context = context

    def _create_operators(self, param, grad):
        group_scale_name = self.group_name + "_scale"
        if group_scale_name not in self.context:
            group_norm_var = layers.sums(input=self.context[self.group_name])
            group_norm_var = layers.sqrt(x=group_norm_var)
            clip_var = self.context[self.group_name + "_clip"]
            group_scale_var = layers.elementwise_div(
                x=clip_var,
                y=layers.elementwise_max(
                    x=clip_var, y=group_norm_var))
            assert group_scale_var.shape == (1, )
            self.context[group_scale_name] = group_scale_var

        new_grad = layers.elementwise_mul(
            x=grad, y=self.context[group_scale_name])

        return param, new_grad


@framework.dygraph_not_support
def set_gradient_clip(clip, param_list=None, program=None):
    """
    Warning:
    
        This API must be used after ``building network`` , and before ``minimize`` , 
        and it may be removed in future releases, so it is not recommended. 
        It is recommended that you use ``minimize(loss, grad_clip=clip)`` for gradient clipping. 
        The clip is an instance of the ``GradientClipBase`` subclass,
        There are three clipping strategies: :ref:`api_fluid_clip_GradientClipByGlobalNorm` , 
        :ref:`api_fluid_clip_GradientClipNorm` , :ref:`api_fluid_clipentclipbyvalue` .
        
    To specify parameters that require gradient clip.

    Args:
        clip (GradientClipBase): An instance of some derived class of GradientClipBase,
                for example :ref:`api_fluid_clip_GradientClipByGlobalNorm` ,
                which describes the type and detailed attributes of required gradient clip.
        param_list (list(Variable), optional): Parameters that require gradient clip.
                It can be a list of parameter or a list of parameter's name.
                Default None, meaning that all parameters in the program will be included.
        program (Program, optional): The program where parameters are located.
                Default None, meaning that using :ref:`api_fluid_default_main_program` .

    Returns:
        None

    Examples:
        .. code-block:: python

            import paddle.fluid as fluid

            def network():
                image = fluid.data(name='image', shape=[
                                   None, 28], dtype='float32')
                param_attr1 = fluid.ParamAttr("fc1_param")
                fc1 = fluid.layers.fc(image, size=10, param_attr=param_attr1)
                param_attr2 = fluid.ParamAttr("fc2_param")
                fc2 = fluid.layers.fc(fc1, size=10, param_attr=param_attr2)
                loss = fluid.layers.reduce_mean(fc2)
                return loss


            # network 1: clip all parameter gradient
            with fluid.program_guard(fluid.Program(), fluid.Program()):
                loss = network()
                fluid.clip.set_gradient_clip(
                    fluid.clip.GradientClipByGlobalNorm(clip_norm=2.0))
                sgd = fluid.optimizer.SGD(learning_rate=1e-3)
                sgd.minimize(loss)

            # network 2: clip parameter gradient by name
            with fluid.program_guard(fluid.Program(), fluid.Program()):
                loss = network()
                fluid.clip.set_gradient_clip(
                    fluid.clip.GradientClipByValue(min=-1.0, max=1.0),
                    param_list=["fc1_param", "fc2_param"])
                sgd = fluid.optimizer.SGD(learning_rate=1e-3)
                sgd.minimize(loss)

            # network 3: clip parameter gradient by variable
            with fluid.program_guard(fluid.Program(), fluid.Program()):
                loss = network()
                param_var1 = fluid.default_main_program().global_block().var("fc1_param")
                param_var2 = fluid.default_main_program().global_block().var("fc2_param")
                fluid.clip.set_gradient_clip(
                    fluid.clip.GradientClipByValue(min=-1.0, max=1.0),
                    param_list=[param_var1, param_var2])
                sgd = fluid.optimizer.SGD(learning_rate=1e-3)
                sgd.minimize(loss)
            
            # network 4: use 'set_gradient_clip' and 'minimize(grad_clip=clip)' together
            with fluid.program_guard(fluid.Program(), fluid.Program()):
                loss = network()
                clip1 = fluid.clip.GradientClipByValue(min=-1.0, max=1.0)
                clip2 = fluid.clip.GradientClipByNorm(clip_norm=1.0)
                # Set the gradient clipping strategy: clip1
                fluid.clip.set_gradient_clip(clip1)
                # Set the gradient clipping strategy: clip2
                sgd = fluid.optimizer.SGD(learning_rate=1e-3)
                sgd.minimize(loss, grad_clip=clip2)
                # 'set_gradient_clip' will not take effect when setting has a conflict, 
                # and the gradient clipping strategy will be 'clip2'
            
            
    """
    warnings.warn("Caution! 'set_gradient_clip' is not recommended "
                  "and may be deprecated in future! "
                  "We recommend a new strategy: clip gradient by "
                  "'optimizer.minimize(loss, grad_clip=clip)'. "
                  "This method can reduce the mistakes, please "
                  "see documention of 'optimzier.minimize'.")

    if not isinstance(clip, GradientClipBase):
        raise TypeError(
            "'clip' should be an instance of GradientClipBase's derived class")
    if program is None:
        program = framework.default_main_program()

    for op in program.block(0).ops:
        if 'op_namescope' in op.all_attrs() and "optimizer" in op.attr(
                "op_namescope"):
            warnings.warn(
                "'minimize' has been invoked before, this will make 'set_gradient_clip' "
                "be ineffective! Please invoke 'set_gradient_clip' before 'minimize'."
            )
            break

    if param_list is None:
        param_list = program.block(0).all_parameters()
    if all(isinstance(elem, six.string_types) for elem in param_list):
        param_list = [program.block(0).var(elem) for elem in param_list]
        raise TypeError(
            "'param_list' should be a list of Parameter or basestring(parameter's name)."
        )

    for param in param_list:
        param.gradient_clip_attr = copy.deepcopy(clip)


def append_gradient_clip_ops(param_grads):
    context = dict()
    for p, g in param_grads:
        if g is None:
            continue
        with p.block.program._optimized_guard(
            [p, g]), framework.name_scope('gradient_clip_@CLIP'):
            clip_attr = getattr(p, 'gradient_clip_attr', None)
            if clip_attr is None:
                return param_grads
            if not isinstance(clip_attr, GradientClipBase):
                raise TypeError(
                    "clip attribute should be an instance of GradientClipBase")

            clip_attr._process_context(context=context, param=p, grad=g)

    res = []
    for p, g in param_grads:
        if g is None:
            continue
        with p.block.program._optimized_guard(
            [p, g]), framework.name_scope('graident_clip_@CLIP'):
            param, new_grad = clip_attr._create_operators(param=p, grad=g)
            res.append([param, new_grad])

    _correct_clip_op_role_var(res)
    return res


# change wrong mapping relation between param & grad in clip op
def _correct_clip_op_role_var(params_grads):
    for param, grad in params_grads:
        if grad is None:
            continue
        for op in param.block.program.global_block().ops:
            if 'op_namescope' in op.all_attrs() and "gradient_clip" in op.attr(
                    "op_namescope"):
                if op.attr('op_role_var'):
                    param_name = op.attr('op_role_var')[0]
                    index = 0
                    for i in range(len(params_grads)):
                        if params_grads[i][0].name == param_name:
                            index = i
                    correct_p_g = [param_name, params_grads[index][1].name]
                    op._set_attr('op_role_var', correct_p_g)


ClipByValue = GradientClipByValue
ClipByNorm = GradientClipByNorm
ClipByGlobalNorm = GradientClipByGlobalNorm
