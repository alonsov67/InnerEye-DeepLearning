#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
import logging
from pathlib import Path
from typing import Any, List, Optional, Union

import numpy as np
import pytest
import torch
from azureml.core import Workspace

from InnerEye.Azure.azure_config import AzureConfig
from InnerEye.Common import fixed_paths
from InnerEye.Common.type_annotations import PathOrString, TupleInt3
from InnerEye.ML.config import SegmentationModelBase
from InnerEye.ML.dataset.full_image_dataset import PatientDatasetSource
from InnerEye.ML.dataset.sample import PatientMetadata, Sample
from InnerEye.ML.photometric_normalization import PhotometricNormalization
from InnerEye.ML.utils import io_util
from InnerEye.ML.utils.config_util import ModelConfigLoader
from InnerEye.ML.utils.io_util import ImageHeader, ImageWithHeader
from InnerEye.ML.utils.ml_util import is_gpu_available
from Tests.fixed_paths_for_tests import full_ml_test_data_path

TEST_CHANNEL_IDS = ["channel1", "channel2"]
TEST_MASK_ID = "mask"
TEST_GT_ID = "region"

machine_has_gpu = is_gpu_available()
no_gpu_available = not machine_has_gpu


def create_dataset_csv_file(csv_string: str, dst: str) -> Path:
    """Creates a dataset.csv in the destination path from the csv_string provided"""
    (Path(dst) / "dataset.csv").write_text(csv_string)
    return Path(dst)


def content_mismatch(actual: Any, expected: Any) -> str:
    """Returns error message for content mismatch."""
    return "Content mismatch. \nActual:\n {}\nExpected:\n {}".format(actual, expected)


def get_nifti_shape(full_path: PathOrString) -> TupleInt3:
    """Returns the size of the image in the given Nifti file, as an (X, Y, Z) tuple."""
    image_with_header = io_util.load_nifti_image(full_path)
    return get_image_shape(image_with_header)


def get_image_shape(image_with_header: ImageWithHeader) -> TupleInt3:
    return image_with_header.image.shape[0], image_with_header.image.shape[1], image_with_header.image.shape[2]


def load_train_and_test_data_channels(patient_ids: List[int],
                                      normalization_fn: PhotometricNormalization) -> List[Sample]:
    if np.any(np.asarray(patient_ids) <= 0):
        raise ValueError("data_items must be >= 0")

    file_name = lambda k, y: full_ml_test_data_path("train_and_test_data") / f"id{k}_{y}.nii.gz"

    get_sample = lambda z: io_util.load_images_from_dataset_source(dataset_source=PatientDatasetSource(
        metadata=PatientMetadata(patient_id=z),
        image_channels=[file_name(z, c) for c in TEST_CHANNEL_IDS],
        mask_channel=file_name(z, TEST_MASK_ID),
        ground_truth_channels=[file_name(z, TEST_GT_ID)]
    ))

    samples = []
    for x in patient_ids:
        sample = get_sample(x)
        sample = Sample(image=normalization_fn.transform(sample.image, sample.mask),
                        mask=sample.mask,
                        labels=sample.labels,
                        metadata=sample.metadata)
        samples.append(sample)

    return samples


def assert_file_contents(full_file: Union[str, Path], expected: Any = None) -> None:
    """
    Checks if the given file contains an expected string
    :param full_file: The path to the file.
    :param expected: The expected contents of the file, as a string.
    """
    logging.info("Checking file {}".format(full_file))
    file_path = full_file if isinstance(full_file, Path) else Path(full_file)
    assert_file_exists(file_path)
    if expected is not None:
        _assert_line(file_path.read_text(), expected)


def assert_file_contents_match_exactly(full_file: Path, expected_file: Path) -> None:
    """
    Checks line by line (ignoring leading and trailing spaces) if the given two files contains the exact same strings
    :param full_file: The path to the file.
    :param expected_file: The expected file.
    """
    with full_file.open() as f1, expected_file.open() as f2:
        for line1, line2 in zip(f1, f2):
            _assert_line(line1, line2)


def _assert_line(actual: str, expected: str) -> None:
    actual = actual.strip()
    expected = expected.strip()
    assert actual == expected, content_mismatch(actual, expected)


def assert_file_exists(file_path: Path) -> None:
    """
    Checks if the given file exists.
    """
    assert file_path.exists(), f"File does not exist: {file_path}"


def assert_nifti_content(full_file: PathOrString,
                         expected_shape: TupleInt3,
                         expected_header: ImageHeader,
                         expected_values: List[int],
                         expected_type: type) -> None:
    """
    Checks if the given nifti file contains the expected unique values, and has the expected type and shape.
    :param full_file: The path to the file.
    :param expected_shape: The expected shape of the image in the nifti file.
    :param expected_header: the expected image header
    :param expected_values: The expected unique values in the image array.
    :param expected_type: The expected type of the stored values.
    """
    if isinstance(full_file, str):
        full_file = Path(full_file)
    assert_file_exists(full_file)
    image_with_header = io_util.load_nifti_image(full_file, None)
    assert image_with_header.image.shape == expected_shape, content_mismatch(image_with_header.image.shape,
                                                                             expected_shape)
    assert image_with_header.image.dtype == np.dtype(expected_type), content_mismatch(image_with_header.image.dtype,
                                                                                      expected_type)
    image = np.unique(image_with_header.image).tolist()
    assert image == expected_values, content_mismatch(image, expected_values)
    assert image_with_header.header == expected_header


def assert_tensors_equal(t1: torch.Tensor, t2: Union[torch.Tensor, List], abs: float = 1e-6) -> None:
    """
    Checks if the shapes of the given tensors is equal, and the values are approximately equal, with a given
    absolute tolerance.
    """
    if isinstance(t2, list):
        t2 = torch.tensor(t2)
    assert t1.shape == t2.shape, "Shapes must match"
    # Alternative is to use torch.allclose here, but that method also checks that datatypes match. This makes
    # writing the test cases more cumbersome.
    v1 = t1.flatten().tolist()
    v2 = t2.flatten().tolist()
    assert v1 == pytest.approx(v2, abs=abs), f"Tensor elements don't match with tolerance {abs}: {v1} != {v2}"


DummyPatientMetadata = PatientMetadata(patient_id=42)


def get_model_loader(namespace: Optional[str] = None) -> ModelConfigLoader[SegmentationModelBase]:
    """
    Returns a ModelConfigLoader for segmentation models, with the given non-default namespace (if not None)
    to search under.
    """
    return ModelConfigLoader[SegmentationModelBase](model_configs_namespace=namespace)


def get_default_azure_config() -> AzureConfig:
    """
    Gets the Azure-related configuration options, using the default settings file train_variables.yaml.
    """
    return AzureConfig.from_yaml(yaml_file_path=fixed_paths.TRAIN_YAML_FILE)


def get_default_workspace() -> Workspace:
    """
    Gets the project's default AzureML workspace.
    :return:
    """
    return get_default_azure_config().get_workspace()