# SPDX-License-Identifier: MIT
# Copyright (c) 2024, Advanced Micro Devices, Inc. All rights reserved.

import warnings
import os
import sys
import shutil

from setuptools import setup, find_packages
from packaging.version import parse, Version
from aiter.jit import core
import torch
from torch.utils.cpp_extension import (
    BuildExtension,
    CppExtension,
    CUDAExtension,
    ROCM_HOME,
    IS_HIP_EXTENSION,
)


this_dir = os.path.dirname(os.path.abspath(__file__))
ck_dir = os.environ.get("CK_DIR", f"{this_dir}/3rdparty/composable_kernel")
bd_dir = f"{this_dir}/build"
blob_dir = f"{bd_dir}/blob"
PACKAGE_NAME = 'aiter'
BUILD_TARGET = os.environ.get("BUILD_TARGET", "auto")

if BUILD_TARGET == "auto":
    if IS_HIP_EXTENSION:
        IS_ROCM = True
    else:
        IS_ROCM = False
else:
    if BUILD_TARGET == "cuda":
        IS_ROCM = False
    elif BUILD_TARGET == "rocm":
        IS_ROCM = True

FORCE_CXX11_ABI = False


def get_hip_version():
    return parse(torch.version.hip.split()[-1].rstrip('-').replace('-', '+'))

def rename_cpp_to_cu(pths):
    return core.rename_cpp_to_cu(pths, bd_dir)


def validate_and_update_archs(archs):
    # List of allowed architectures
    allowed_archs = ["native", "gfx90a",
                     "gfx940", "gfx941", "gfx942", "gfx1100"]

    # Validate if each element in archs is in allowed_archs
    assert all(
        arch in allowed_archs for arch in archs
    ), f"One of GPU archs of {archs} is invalid or not supported"


ext_modules = []

if IS_ROCM:
    # use codegen get code dispatch
    if not os.path.exists(bd_dir):
        os.makedirs(bd_dir)
    if not os.path.exists(blob_dir):
        os.makedirs(blob_dir)

    print(f"\n\ntorch.__version__  = {torch.__version__}\n\n")
    TORCH_MAJOR = int(torch.__version__.split(".")[0])
    TORCH_MINOR = int(torch.__version__.split(".")[1])

    # Check, if ATen/CUDAGeneratorImpl.h is found, otherwise use ATen/cuda/CUDAGeneratorImpl.h
    # See https://github.com/pytorch/pytorch/pull/70650
    generator_flag = []
    torch_dir = torch.__path__[0]
    if os.path.exists(os.path.join(torch_dir, "include", "ATen", "CUDAGeneratorImpl.h")):
        generator_flag.append("-DOLD_GENERATOR_PATH")
    assert os.path.exists(
        ck_dir), f'CK is needed by aiter, please make sure clone by "git clone --recursive https://github.com/ROCm/aiter.git" or "git submodule sync ; git submodule update --init --recursive"'

    cc_flag = []

    archs = os.getenv("GPU_ARCHS", "native").split(";")
    validate_and_update_archs(archs)

    cc_flag = [f"--offload-arch={arch}" for arch in archs]
    hip_version = get_hip_version()
    cc_flag += [
        "-mllvm", "-enable-post-misched=0",
        "-mllvm", "-amdgpu-early-inline-all=true",
        "-mllvm", "-amdgpu-function-calls=false",
        "-mllvm", "--amdgpu-kernarg-preload-count=16",
        "-mllvm", "-amdgpu-coerce-illegal-types=1",
        "-Wno-unused-result",
        "-Wno-switch-bool",
        "-Wno-vla-cxx-extension",
        "-Wno-undefined-func-template",
        "-fgpu-flush-denormals-to-zero",
    ]

    # Imitate https://github.com/ROCm/composable_kernel/blob/c8b6b64240e840a7decf76dfaa13c37da5294c4a/CMakeLists.txt#L190-L214
    hip_version = get_hip_version()
    if hip_version > Version('5.7.23302'):
        cc_flag += ["-fno-offload-uniform-block"]
    if hip_version > Version('6.1.40090'):
        cc_flag += ["-mllvm", "-enable-post-misched=0"]
    if hip_version > Version('6.2.41132'):
        cc_flag += ["-mllvm", "-amdgpu-early-inline-all=true",
                    "-mllvm", "-amdgpu-function-calls=false"]
    if hip_version > Version('6.2.41133') and hip_version < Version('6.3.00000'):
        cc_flag += ["-mllvm", "-amdgpu-coerce-illegal-types=1"]

    # HACK: The compiler flag -D_GLIBCXX_USE_CXX11_ABI is set to be the same as
    # torch._C._GLIBCXX_USE_CXX11_ABI
    # https://github.com/pytorch/pytorch/blob/8472c24e3b5b60150096486616d98b7bea01500b/torch/utils/cpp_extension.py#L920
    if FORCE_CXX11_ABI:
        torch._C._GLIBCXX_USE_CXX11_ABI = True

    if int(os.environ.get("PREBUILD_KERNELS", 0)) == 1:
        exclude_ops=["module_mha_fwd",
                     "module_mha_varlen_fwd",
                     "module_mha_bwd",
                     "module_mha_varlen_bwd"]
        all_opts_args_build = core.get_args_of_build("all", exclue=exclude_ops)
        # remove pybind, because there are already duplicates in rocm_opt
        new_list=[el for el in all_opts_args_build["srcs"] if "pybind.cu" not in el]
        all_opts_args_build["srcs"] = new_list

        core.build_module(md_name = "aiter_",
                    srcs = all_opts_args_build["srcs"] + [f"{this_dir}/csrc"],
                    flags_extra_cc = all_opts_args_build["flags_extra_cc"],
                    flags_extra_hip = all_opts_args_build["flags_extra_hip"],
                    blob_gen_cmd = all_opts_args_build["blob_gen_cmd"],
                    extra_include = all_opts_args_build["extra_include"],
                    extra_ldflags = None,
                    verbose = False,
        )

    # ########## gradlib for tuned GEMM start here
    renamed_sources = rename_cpp_to_cu([f"{this_dir}/gradlib/csrc"])
    include_dirs = []
    ext_modules.append(
        CUDAExtension(
            name='rocsolidxgemm_',
            sources=[f'{bd_dir}/rocsolgemm.cu'],
            include_dirs=include_dirs,
            # add additional libraries argument for hipblaslt
            libraries=['rocblas'],
            extra_compile_args={
                'cxx': [
                    '-O3',
                    '-DLEGACY_HIPBLAS_DIRECT=ON',
                ],
                'nvcc': [
                    '-O3',
                    '-U__CUDA_NO_HALF_OPERATORS__',
                    '-U__CUDA_NO_HALF_CONVERSIONS__',
                    "-ftemplate-depth=1024",
                    '-DLEGACY_HIPBLAS_DIRECT=ON',
                ] + cc_flag
            }))
    ext_modules.append(
        CUDAExtension(
            name='hipbsolidxgemm_',
            sources=[f'{bd_dir}/hipbsolgemm.cu'],
            include_dirs=include_dirs,
            # add additional libraries argument for hipblaslt
            libraries=['hipblaslt'],
            extra_compile_args={
                'cxx': [
                    '-O3',
                    '-DLEGACY_HIPBLAS_DIRECT=ON',
                ],
                'nvcc': [
                    '-O3',
                    '-U__CUDA_NO_HALF_OPERATORS__',
                    '-U__CUDA_NO_HALF_CONVERSIONS__',
                    "-ftemplate-depth=1024",
                    '-DLEGACY_HIPBLAS_DIRECT=ON',
                ] + cc_flag + ['-DENABLE_TORCH_FP8'] if hasattr(torch, 'float8_e4m3fnuz') else []
            }))
    # ########## gradlib for tuned GEMM end here
else:
    raise NotImplementedError("Only ROCM is supported")


if os.path.exists("aiter_meta") and os.path.isdir("aiter_meta"):
    shutil.rmtree("aiter_meta")
## link "3rdparty", "hsa", "csrc" into "aiter_meta"
shutil.copytree("3rdparty", "aiter_meta/3rdparty")
shutil.copytree("hsa", "aiter_meta/hsa")
shutil.copytree("csrc", "aiter_meta/csrc")
os.chmod("aiter_meta", 0o777)



class NinjaBuildExtension(BuildExtension):
    def __init__(self, *args, **kwargs) -> None:
        # calculate the maximum allowed NUM_JOBS based on cores
        max_num_jobs_cores = max(1, os.cpu_count()*0.8)
        if int(os.environ.get("MAX_JOBS", '1')) < max_num_jobs_cores:
            import psutil
            # calculate the maximum allowed NUM_JOBS based on free memory
            free_memory_gb = psutil.virtual_memory().available / \
                (1024 ** 3)  # free memory in GB
            # each JOB peak memory cost is ~8-9GB when threads = 4
            max_num_jobs_memory = int(free_memory_gb / 9)

            # pick lower value of jobs based on cores vs memory metric to minimize oom and swap usage during compilation
            max_jobs = int(
                max(1, min(max_num_jobs_cores, max_num_jobs_memory)))
            os.environ["MAX_JOBS"] = str(max_jobs)

        super().__init__(*args, **kwargs)


setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=["aiter_meta","aiter"],
    include_package_data=True,
    package_data={
        '': ['*'],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD License",
        "Operating System :: Unix",
    ],
    ext_modules=ext_modules,
    cmdclass={"build_ext": NinjaBuildExtension},
    python_requires=">=3.8",
    # install_requires=[
    #     "torch",
    # ],
    setup_requires=[
        "packaging",
        "psutil",
        "ninja",
    ],
)

if os.path.exists("aiter_meta") and os.path.isdir("aiter_meta"):
    shutil.rmtree("aiter_meta")
# if os.path.exists(bd_dir):
#     shutil.rmtree(bd_dir)
# if os.path.exists(blob_dir):
#     shutil.rmtree(blob_dir)
# if os.path.exists(f"./.eggs"):
#     shutil.rmtree(f"./.eggs")
# if os.path.exists(f"./{PACKAGE_NAME}.egg-info"):
#     shutil.rmtree(f"./{PACKAGE_NAME}.egg-info")
# if os.path.exists('./build'):
#     shutil.rmtree(f"./build")
