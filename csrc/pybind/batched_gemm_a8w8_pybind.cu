// SPDX-License-Identifier: MIT
// Copyright (c) 2024, Advanced Micro Devices, Inc. All rights reserved.
#include "batched_gemm_a8w8.h"

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)
{
    m.def("batched_gemm_a8w8", &batched_gemm_a8w8, "batched_gemm_a8w8", py::arg("XQ"), py::arg("WQ"),
          py::arg("x_scale"), py::arg("w_scale"), py::arg("Out"),
          py::arg("bias") = std::nullopt, py::arg("splitK") = 0);
}
