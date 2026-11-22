from __future__ import annotations

import re

ALIASES = {
    "sklearn": "scikit-learn",
    "pytorch": "torch",
    "torch-cpu": "torch",
    "torch-gpu": "torch",
    "tensorflow-gpu": "tensorflow",
    "tensorflow-cpu": "tensorflow",
    "tf-nightly": "tensorflow",
    "opencv-python": "opencv",
    "opencv-python-headless": "opencv",
    "opencv-contrib-python": "opencv",
    "opencv-contrib-python-headless": "opencv",
    "cv2": "opencv",
    "pil": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "jaxlib": "jax",
    "faiss-cpu": "faiss",
    "faiss-gpu": "faiss",
    "onnxruntime-gpu": "onnxruntime",
    "pytorch-lightning": "lightning",
    "wandb-core": "wandb",
}

DROP = {
    "",
    "python",
    "pip",
    "wheel",
    "packaging",
    "typing-extensions",
    "setuptools-scm",
    "cudatoolkit",
    "cudnn",
    "pytorch-mutex",
    "blas",
    "mkl",
    "ca-certificates",
    "openssl",
    "libgcc-ng",
    "libstdcxx-ng",
    "libffi",
    "ncurses",
    "readline",
    "tzdata",
}

TRANSITIVE = {
    "urllib3",
    "idna",
    "charset-normalizer",
    "cycler",
    "kiwisolver",
    "pyparsing",
    "fonttools",
    "contourpy",
    "python-dateutil",
    "pytz",
    "filelock",
    "fsspec",
    "mpmath",
    "threadpoolctl",
    "tomli",
    "colorama",
    "platformdirs",
}

CONDA_SYSTEM = {
    "libgcc-mutex",
    "openmp-mutex",
    "libgomp",
    "libpng",
    "libtiff",
    "libwebp",
    "mkl-fft",
    "mkl-random",
    "mkl-service",
    "numpy-base",
    "jpeg",
    "icu",
    "freetype",
}

STDLIB_BACKPORTS = {"argparse", "dataclasses", "typing", "enum34", "futures", "pathlib"}


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip().strip("-")


def normalize_package(name: str) -> tuple[str | None, str]:
    normalized = pep503(name)
    if normalized.startswith(("nvidia-", "cuda-")) and normalized not in ALIASES:
        return None, "runtime_or_system_package"
    if re.fullmatch(r"cupy-cuda\w+", normalized):
        normalized = "cupy"
    if re.fullmatch(r"mxnet-cu\w+", normalized):
        normalized = "mxnet"
    normalized = ALIASES.get(normalized, normalized)
    if normalized in DROP:
        return None, "packaging_or_system_component"
    if normalized in TRANSITIVE:
        return None, "known_transitive_dependency"
    if normalized in CONDA_SYSTEM:
        return None, "conda_system_library"
    if normalized in STDLIB_BACKPORTS:
        return None, "stdlib_or_backport"
    return normalized, "normalized" if normalized != name else "unchanged"
