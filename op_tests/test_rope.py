# SPDX-License-Identifier: MIT
# Copyright (c) 2025, Advanced Micro Devices, Inc. All rights reserved.

import torch
import torch.nn.functional as F
import aiter
from aiter.test_common import checkAllclose, perftest
import itertools


@perftest()
def hip_rope_fwd(input, freqs, transpose_output):
    return aiter.rope_fwd(input, freqs, transpose_output)

@perftest()
def hip_rope_bwd(output_grads, freqs, transpose_output):
    return aiter.rope_bwd(output_grads, freqs, transpose_output)

@perftest()
def hip_rope_cached_fwd(input, cos, sin, transpose_output):
    return aiter.rope_cached_fwd(input, cos, sin, transpose_output)

@perftest()
def hip_rope_cached_bwd(output_grads, cos, sin, transpose_output):
    return aiter.rope_cached_bwd(output_grads, cos, sin, transpose_output)

@perftest()
def hip_rope_thd_fwd(input, cu_seqlens, freqs):
    return aiter.rope_thd_fwd(input, cu_seqlens, freqs)

@perftest()
def hip_rope_thd_bwd(output_grads, cu_seqlens, freqs):
    return aiter.rope_thd_bwd(output_grads, cu_seqlens, freqs)

@perftest()
def hip_rope_2d_fwd(input, height, width, cos_h, sin_h, cos_w, sin_w):
    return aiter.rope_2d_fwd(input, height, width, cos_h, sin_h, cos_w, sin_w)

@perftest()
def hip_rope_2d_bwd(output_grads, height, width, cos_h, sin_h, cos_w, sin_w):
    return aiter.rope_2d_bwd(output_grads, height, width, cos_h, sin_h, cos_w, sin_w)


def rotate_half(x):
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)

def ref_rope_sbhd_fwd(x, freqs):
    freqs_dim = freqs.shape[-1]
    x, x_forward = x[..., :freqs_dim], x[..., freqs_dim:]
    x_embed = (x * torch.cos(freqs)) + (rotate_half(x) * torch.sin(freqs))
    return torch.cat((x_embed.to(dtype=x.dtype), x_forward), dim=-1)

def ref_rope_thd_fwd(x, cu_seqlens, freqs):
    seqlens = (cu_seqlens[1:] - cu_seqlens[:-1]).tolist()
    x_embed = torch.cat([
        ref_rope_sbhd_fwd(xi.unsqueeze(1), freqs[: xi.size(0)])
        for xi in torch.split(x, seqlens)
    ])
    return x_embed.squeeze(1)

def ref_rope_2d_fwd(x, size_h, size_w, cos_h, sin_h, cos_w, sin_w):
    s, b, h, d = x.shape
    x = x.view(s, size_h, size_w, h, d)
    x1, x2 = x.chunk(2, dim=-1)
    cos_h = cos_h[:, :size_h].unsqueeze(2)  # [1, H, 1, 1, D//2]
    sin_h = sin_h[:, :size_h].unsqueeze(2)  # [1, H, 1, 1, D//2]
    x1 = (x1 * cos_h) + (rotate_half(x1) * sin_h)
    cos_w = cos_w[:, :size_w].unsqueeze(1)  # [1, 1, W, 1, D//2]
    sin_w = sin_w[:, :size_w].unsqueeze(1)  # [1, 1, W, 1, D//2]
    x2 = (x2 * cos_w) + (rotate_half(x2) * sin_w)
    return torch.cat([x1, x2], dim=-1).view(s, b, h, d).to(dtype=x.dtype)


def test_rope_sbhd(input, freqs, grad, transpose_output):
    input_msg = f"dtype: {input.dtype}, freq_dtype: {freqs.dtype}, dim_input: {str(input.shape):<20}, dim_freqs: {str(freqs.shape):<20}, transpose_output: {transpose_output}"

    ref = ref_rope_sbhd_fwd(input, freqs)
    ref.backward(grad)

    cos   = torch.cos(freqs)
    sin   = torch.sin(freqs)

    hip_fwd,        hip_fwd_avg        = hip_rope_fwd(input, freqs, transpose_output)
    hip_bwd,        hip_bwd_avg        = hip_rope_bwd(grad, freqs, transpose_output)
    hip_cached_fwd, hip_cached_fwd_avg = hip_rope_cached_fwd(input, cos, sin, transpose_output)
    hip_cached_bwd, hip_cached_bwd_avg = hip_rope_cached_bwd(grad, cos, sin, transpose_output)

    checkAllclose(ref,        hip_fwd,        msg=f"rope_fwd - avg: {hip_fwd_avg:<8.2f} us - {input_msg}\n")
    checkAllclose(input.grad, hip_bwd,        msg=f"rope_bwd - avg: {hip_bwd_avg:<8.2f} us - {input_msg}\n")
    checkAllclose(ref,        hip_cached_fwd, msg=f"rope_cached_fwd - avg: {hip_cached_fwd_avg:<8.2f} us - {input_msg}\n")
    checkAllclose(input.grad, hip_cached_bwd, msg=f"rope_cached_bwd - avg: {hip_cached_bwd_avg:<8.2f} us - {input_msg}\n")


def test_rope_thd(input, cu_seqlens, freqs, grad):
    torch.set_printoptions(profile="full")
    input_msg = f"dtype: {input.dtype}, freq_dtype: {freqs.dtype}, dim_input: {str(input.shape):<20}, dim_freqs: {str(freqs.shape):<20}, cu_seqlens: {cu_seqlens}"
    torch.set_printoptions(profile="default")

    ref = ref_rope_thd_fwd(input, cu_seqlens, freqs)
    ref.backward(grad)

    hip_fwd, hip_fwd_avg = hip_rope_thd_fwd(input, cu_seqlens, freqs)
    hip_bwd, hip_bwd_avg = hip_rope_thd_bwd(grad, cu_seqlens, freqs)

    checkAllclose(ref,        hip_fwd, msg=f"rope_thd_fwd - avg: {hip_fwd_avg:<8.2f} us - {input_msg}\n")
    checkAllclose(input.grad, hip_bwd, msg=f"rope_thd_bwd - avg: {hip_bwd_avg:<8.2f} us - {input_msg}\n")


def test_rope_2d(input, height, width, freqs_h, freqs_w, grad):
    input_msg = f"dtype: {input.dtype}, freq_dtype: {freqs_h.dtype}, dim_input: {str(input.shape):<20}, dim_freqs: {str(freqs_h.shape):<20}"

    cos_h = freqs_h.cos()
    sin_h = freqs_h.sin()
    cos_w = freqs_w.cos()
    sin_w = freqs_w.sin()

    ref = ref_rope_2d_fwd(input, height, width, cos_h, sin_h, cos_w, sin_w)
    ref.backward(grad)

    hip_fwd, hip_fwd_avg = hip_rope_2d_fwd(input, cos_h, sin_h, cos_w, sin_w, height, width)
    hip_bwd, hip_bwd_avg = hip_rope_2d_bwd(grad, cos_h, sin_h, cos_w, sin_w, height, width)

    checkAllclose(ref,        hip_fwd, msg=f"rope_2d_fwd - avg: {hip_fwd_avg:<8.2f} us - {input_msg}\n")
    checkAllclose(input.grad, hip_bwd, msg=f"rope_2d_bwd - avg: {hip_bwd_avg:<8.2f} us - {input_msg}\n")


if __name__ == "__main__":
    dtype_ = (torch.float, torch.float16, torch.bfloat16)
    transpose_output_ = (False, True)
    batch_size_ = (1, 2, 4)
    seq_size_ = (1024, 2048, 4096)
    head_size_ = (32, 64)
    hidden_dim_ = (128, 256)
    rotary_percent_ = (0.5, 1.0)
    height_ = (32, 64)
    width_ = (32, 64)
    margin_ = (0, 1, 3)

    # Test sbhd format for both cached and uncached
    for (dtype, fdtype,
         transpose_output,
         rotary_percent,
         b, s, h, d
    ) in itertools.product(
        dtype_, dtype_,
        transpose_output_,
        rotary_percent_,
        batch_size_, seq_size_, head_size_, hidden_dim_
    ):
        input = torch.randn((s, b, h, d), dtype=dtype, device="cuda", requires_grad=True)
        freqs = torch.randn((s, 1, 1, int(d * rotary_percent)), dtype=fdtype, device="cuda")
        grad  = torch.randn((s, b, h, d), dtype=dtype, device="cuda")
        test_rope_sbhd(input, freqs, grad, transpose_output)

    # Test thd format for uncached
    cu_seqlens = torch.tensor([0, 100, 102, 128, 233, 456, 460, 711, 1024, 1536, 1739, 1888, 2000, 2001, 2048],
                              dtype=torch.int32, device="cuda")
    for (dtype, fdtype,
         rotary_percent,
         h, d
    ) in itertools.product(
        dtype_, dtype_,
        rotary_percent_,
        head_size_, hidden_dim_
    ):
        input = torch.randn((cu_seqlens[-1], h, d), dtype=dtype, device="cuda", requires_grad=True)
        freqs = torch.randn((cu_seqlens[-1], 1, 1, int(d * rotary_percent)), dtype=fdtype, device="cuda")
        grad  = torch.randn((cu_seqlens[-1], h, d), dtype=dtype, device="cuda")
        test_rope_thd(input, cu_seqlens, freqs, grad)

    # Test 2d image format for cached
    for (dtype, fdtype,
         b, h, d,
         height, width, margin
    ) in itertools.product(
        dtype_, dtype_,
        batch_size_, head_size_, hidden_dim_,
        height_, width_, margin_
    ):
        input   = torch.randn((b, height * width, h, d), dtype=dtype, device="cuda", requires_grad=True)
        freqs_h = torch.randn((1, height + margin, 1, d // 2), dtype=fdtype, device="cuda")
        freqs_w = torch.randn((1, width + margin, 1, d // 2), dtype=fdtype, device="cuda")
        grad    = torch.randn((b, height * width, h, d), dtype=dtype, device="cuda")
        test_rope_2d(input, height, width, freqs_h, freqs_w, grad)
