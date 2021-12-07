from collections import defaultdict
import copy
import time

import torch
import torch.fx
from torch.ao.quantization.quantize_fx import prepare_fx, convert_fx
from torch.fx.experimental.fx2trt.fx2trt import TRTInterpreter, InputTensorSpec, TRTModule
import torch.fx.experimental.fx_acc.acc_tracer as acc_tracer
from torch.fx.passes import shape_prop
from torch.fx.experimental.normalize import NormalizeArgs

import torchvision.models as models

import tensorrt as trt

from torch.ao import sparsity

rn18 = models.resnet18().eval()


def get_sparse_params(sparsifier):
    sparse_params = defaultdict(dict)
    for sparse_args in sparsifier.module_groups:
        w = sparse_args['module'].weight
        current_params = sparse_params['fqn']
        for key in ('sparsity_level', 'zeros_per_block', 'sparse_block_shape'):
            current_params[key] = sparse_args[key]
        current_params['true_sparsity_level'] = (w == 0).float().mean()
        current_params['nvidia_style_sparsity'] = False

        if (
            current_params['sparsity_level'] == 1.0 and
            current_params['zeros_per_block'] == 2 and
            current_params['sparse_block_shape'] == (1, 4)
           ):
            current_params['nvidia_style_sparsity'] = True
            for row in w:
                for col_idx in range(0, len(row), 4):
                    if (row[col_idx:col_idx + 4] == 0).sum() < 2:
                        current_params['nvidia_style_sparsity'] = False
                        break
                else:
                    continue
                break
        return sparse_params


def post_training_sparsify(model: torch.nn.Module, sparse_params=None, verbose=False):
    if sparse_params is None:
        sparse_params = {
            'sparsity_level': 1.0,
            'sparse_block_shape': (1, 4),
            'zeros_per_block': 2,
        }

    if verbose:
        logger = trt.Logger(trt.Logger.INFO)
        logger.log(logger.INFO, f"Running post-training sparsity with the following parameters: {sparse_params}")

    sparsifier = sparsity.WeightNormSparsifier(**sparse_params)
    sparsifier.prepare(model, config=None)
    sparsifier.step()
    sparsifier.squash_mask()

    if verbose:
        sparse_params = get_sparse_params(sparsifier)
        logger.log(logger.INFO, f"Sparse params: {sparse_params}")

    return model


def build_fp16_trt(rn18, device=None, logger_level=None):
    rn18 = copy.deepcopy(rn18)
    rn18 = acc_tracer.trace(rn18, [torch.randn(1, 3, 224, 224)])
    interp = TRTInterpreter(
        rn18, [InputTensorSpec(torch.Size([3, 224, 224]), torch.float, has_batch_dim=False)],
        logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=True)
    return TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)


@torch.no_grad()
def build_sparse_fp16_trt(rn18, sparse_params=None, logger_level=None):
    r"""Builds a sparse model based on the rn18.

    Args:
        sparse_params: Sparsity parameters to pass to the sparsifier.

    Note: Once the model is sparsified, it stores the sparse tensors as dense
          with whole bunch of zeros. To see the benefits of sparsity, use
          the A100 sparse kernels with the following sparse parameters:
          - sparsity_level=1.0
          - sparse_block_shape=(1, 4)
          - zeros_per_block=2
          When using on non-specialized CUDA kernels such as cuSparse, there
          will be no latency benefits.
    """
    rn18 = copy.deepcopy(rn18)

    # Sparsify the model
    post_training_sparsify(rn18, verbose=True)

    # Fx the model
    rn18 = acc_tracer.trace(rn18, [torch.randn(1, 3, 224, 224)])
    interp = TRTInterpreter(
        rn18, [InputTensorSpec(torch.Size([3, 224, 224]), torch.float, has_batch_dim=False)],
        logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=True, sparse_weight=True)
    return TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)


@torch.no_grad()
def build_int8_trt(rn18, logger_level=None):
    rn18 = copy.deepcopy(rn18)
    data = torch.randn(1, 3, 224, 224)
    # data = torch.randn(1, 32)
    # data = torch.randn(1, 64, 10, 10)
    # TensorRT only supports symmetric quantization
    qconfig = torch.ao.quantization.QConfig(
        activation=torch.ao.quantization.observer.HistogramObserver.with_args(
            qscheme=torch.per_tensor_symmetric, dtype=torch.qint8
        ),
        # weight=torch.ao.quantization.default_weight_observer
        # uncomment to check per channel quant works
        weight=torch.quantization.default_per_channel_weight_observer
    )
    prepared = prepare_fx(rn18, {"": qconfig})
    for _ in range(10):
        prepared(data)
    quantized_rn18 = convert_fx(prepared, is_reference=True)
    ref_res = quantized_rn18(data)
    print("quantized model:", quantized_rn18)

    quantized_rn18 = acc_tracer.trace(quantized_rn18, [data])  # type: ignore[assignment]
    interp = TRTInterpreter(
        quantized_rn18,
        [InputTensorSpec(torch.Size([-1, *data.shape[1:]]), torch.float,
                         shape_ranges=[((1, 3, 224, 224), (5, 3, 224, 224), (10, 3, 224, 224))], has_batch_dim=True)],
        explicit_batch_dimension=True, explicit_precision=True, logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=False, int8_mode=True)
    trt_mod = TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)
    trt_res = trt_mod(data.cuda())
    print("explicit quant result diff max", torch.max(ref_res - trt_res.cpu()))
    return trt_mod


@torch.no_grad()
def build_sparse_int8_trt(rn18, logger_level=None):
    rn18 = copy.deepcopy(rn18)

    # Sparsify the model
    post_training_sparsify(rn18, verbose=True)

    data = torch.randn(1, 3, 224, 224)
    # TensorRT only supports symmetric quantization
    qconfig = torch.ao.quantization.QConfig(
        activation=torch.ao.quantization.observer.HistogramObserver.with_args(
            qscheme=torch.per_tensor_symmetric, dtype=torch.qint8
        ),
        # weight=torch.ao.quantization.default_weight_observer
        # uncomment to check per channel quant works
        weight=torch.quantization.default_per_channel_weight_observer
    )
    prepared = prepare_fx(rn18, {"": qconfig})
    for _ in range(10):
        prepared(data)
    quantized_rn18 = convert_fx(prepared, is_reference=True)
    ref_res = quantized_rn18(data)
    print("quantized model:", quantized_rn18)

    quantized_rn18 = acc_tracer.trace(quantized_rn18, [data])  # type: ignore[assignment]
    interp = TRTInterpreter(
        quantized_rn18,
        [InputTensorSpec(torch.Size([-1, *data.shape[1:]]), torch.float,
                         shape_ranges=[((1, 3, 224, 224), (5, 3, 224, 224), (10, 3, 224, 224))], has_batch_dim=True)],
        explicit_batch_dimension=True, explicit_precision=True, logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=False, int8_mode=True, sparse_weight=True)
    trt_mod = TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)
    trt_res = trt_mod(data.cuda())
    print("explicit quant result diff max", torch.max(ref_res - trt_res.cpu()))
    return trt_mod


@torch.no_grad()
def build_int8_trt_implicit_quant(rn18, logger_level=None):
    rn18 = copy.deepcopy(rn18)
    data = torch.randn(1, 3, 224, 224)
    # Quantization
    qconfig = torch.ao.quantization.QConfig(
        activation=torch.ao.quantization.observer.HistogramObserver.with_args(
            qscheme=torch.per_tensor_symmetric, reduce_range=True
        ),
        weight=torch.ao.quantization.default_per_channel_weight_observer
    )
    prepared = prepare_fx(rn18, {"": qconfig})
    for _ in range(10):
        prepared(data)
    quantized_rn18 = convert_fx(prepared)
    ref_res = quantized_rn18(data)

    # Build trt int8 model
    traced_rn18 = torch.fx.symbolic_trace(quantized_rn18)
    shape_prop.ShapeProp(traced_rn18).propagate(data)
    traced_rn18 = NormalizeArgs(traced_rn18).transform()
    interp = TRTInterpreter(traced_rn18, InputTensorSpec.from_tensors([data]), logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=False, int8_mode=True, strict_type_constraints=True)
    trt_mod = TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)
    trt_res = trt_mod(data.cuda())
    print("implicit quant result diff max", torch.max(ref_res - trt_res.cpu()))
    return trt_mod


@torch.no_grad()
def build_sparse_int8_trt_implicit_quant(rn18, logger_level):
    rn18 = copy.deepcopy(rn18)

    # Sparsify the model
    post_training_sparsify(rn18, verbose=True)

    data = torch.randn(1, 3, 224, 224)
    # Quantization
    qconfig = torch.ao.quantization.QConfig(
        activation=torch.ao.quantization.observer.HistogramObserver.with_args(
            qscheme=torch.per_tensor_symmetric, reduce_range=True
        ),
        weight=torch.ao.quantization.default_per_channel_weight_observer
    )
    prepared = prepare_fx(rn18, {"": qconfig})
    for _ in range(10):
        prepared(data)
    quantized_rn18 = convert_fx(prepared)
    ref_res = quantized_rn18(data)

    # Build trt int8 model
    traced_rn18 = torch.fx.symbolic_trace(quantized_rn18)
    shape_prop.ShapeProp(traced_rn18).propagate(data)
    traced_rn18 = NormalizeArgs(traced_rn18).transform()
    interp = TRTInterpreter(traced_rn18, InputTensorSpec.from_tensors([data]), logger_level=logger_level)
    interpreter_result = interp.run(fp16_mode=False, int8_mode=True, sparse_weight=True, strict_type_constraints=True)
    trt_mod = TRTModule(interpreter_result.engine, interpreter_result.input_names, interpreter_result.output_names)
    trt_res = trt_mod(data.cuda())
    print("implicit quant result diff max", torch.max(ref_res - trt_res.cpu()))
    return trt_mod


fp16_trt = build_fp16_trt(rn18, logger_level=trt.Logger.VERBOSE)
sparse_fp16_trt = build_sparse_fp16_trt(rn18, logger_level=trt.Logger.VERBOSE)
int8_trt = build_int8_trt(rn18, logger_level=trt.Logger.VERBOSE)
sparse_int8_trt = build_sparse_int8_trt(rn18, logger_level=trt.Logger.VERBOSE)
implicit_int8_trt = build_int8_trt_implicit_quant(rn18, logger_level=trt.Logger.VERBOSE)
implicit_sparse_int8_trt = build_sparse_int8_trt_implicit_quant(rn18, logger_level=trt.Logger.VERBOSE)

rn18 = rn18.cuda()

x = torch.randn(5, 3, 224, 224, device="cuda")
# x = torch.randn(1, 32, device="cuda")

NITER = 100

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    fp16_trt(x)
    torch.cuda.synchronize()
print('trt fp16 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    sparse_fp16_trt(x)
    torch.cuda.synchronize()
print('trt sparse fp16 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    int8_trt(x)
    torch.cuda.synchronize()
print('trt int8 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    sparse_int8_trt(x)
    torch.cuda.synchronize()
print('trt sparseint8 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    implicit_int8_trt(x)
    torch.cuda.synchronize()
print('trt implicit int8 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    implicit_sparse_int8_trt(x)
    torch.cuda.synchronize()
print('trt implicit sparse int8 time (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
for _ in range(NITER):
    rn18(x)
    torch.cuda.synchronize()
print('PyTorch time (CUDA) (ms/iter)', (time.time() - s) / NITER * 1000)

torch.cuda.synchronize()
s = time.time()
rn18 = rn18.cpu()
x = x.cpu()
for _ in range(NITER):
    rn18(x)
print('PyTorch time (CPU) (ms/iter)', (time.time() - s) / NITER * 1000)
