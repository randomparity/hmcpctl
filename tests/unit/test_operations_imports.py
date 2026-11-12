"""Import-order regressions for operation-module dependency boundaries."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "modules",
    [
        ("hmcpctl.operations.lpar", "hmcpctl.operations.virtualization.vnic"),
        ("hmcpctl.operations.virtualization.vnic", "hmcpctl.operations.lpar"),
        ("hmcpctl.ssh.lpar", "hmcpctl.ssh.profiles"),
        ("hmcpctl.ssh.profiles", "hmcpctl.ssh.lpar"),
    ],
)
def test_sibling_modules_import_in_either_order(
    modules: tuple[str, str],
) -> None:
    imports = "; ".join(f"import {module}" for module in modules)

    completed = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "module", ["hmcpctl.operations.logical.store", "hmcpctl.operations.logical.engine"]
)
def test_logical_operations_import_without_application_dependencies(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoApplicationPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'fastmcp', 'mcp', 'typer', 'rich'}}:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, NoApplicationPackages())
importlib.import_module({module!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_power_provider_can_be_reloaded_without_initializing_the_engine():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; from hmcpctl.operations.lpar import power; importlib.reload(power)",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "module", ["hmcpctl.documents.lpar_io", "hmcpctl.documents.lpar_creation_memory"]
)
def test_document_models_import_without_ssh_execution(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoSSHExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {{"hmcpctl.ssh.transport", "hmcpctl.ssh.profiles", "asyncssh"}}:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, NoSSHExecution())
importlib.import_module({module!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("module", ["security_policy", "vtpm"])
def test_vios_authorization_consumers_import_without_configuration(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoConfiguration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.lpar.configuration":
            raise ModuleNotFoundError("configuration dependency forbidden")

sys.meta_path.insert(0, NoConfiguration())
importlib.import_module("hmcpctl.operations.lpar.{module}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("module", ["hmcpctl.ssh.lpar_io", "hmcpctl.ssh.lpar"])
def test_ssh_creation_grammar_imports_without_operations(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoOperations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations" or fullname.startswith("hmcpctl.operations."):
            raise ModuleNotFoundError("operation dependency forbidden")

sys.meta_path.insert(0, NoOperations())
importlib.import_module({module!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "module",
    ["provisioning.operation", "decommission.operation", "reconfiguration.core"],
)
def test_lifecycle_connection_factory_does_not_import_power_workflow(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoPowerWorkflow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.lpar.power":
            raise ModuleNotFoundError("power workflow dependency forbidden")

sys.meta_path.insert(0, NoPowerWorkflow())
importlib.import_module("hmcpctl.operations.lpar.{module}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_reconfiguration_imports_without_provisioning_workflow():
    script = """
import importlib
import importlib.abc
import sys

class NoProvisioningWorkflow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.lpar.provisioning.operation":
            raise ModuleNotFoundError("provisioning workflow dependency forbidden")

sys.meta_path.insert(0, NoProvisioningWorkflow())
importlib.import_module("hmcpctl.operations.lpar.reconfiguration.core")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("module", ["processors.current", "processors.move"])
def test_partition_read_without_collectors(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoCollectors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.inventory.composite":
            raise ModuleNotFoundError("composite collector dependency forbidden")

sys.meta_path.insert(0, NoCollectors())
importlib.import_module("hmcpctl.operations.lpar.{module}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("module", ["processors.current", "processors.move"])
def test_partition_read_without_memory_mutation(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoMemoryMutation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.lpar.memory.current":
            raise ModuleNotFoundError("memory mutation dependency forbidden")

sys.meta_path.insert(0, NoMemoryMutation())
importlib.import_module("hmcpctl.operations.lpar.{module}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_lifecycle_mutations_import_without_creation_coordination():
    script = """
import importlib
import importlib.abc
import sys

class NoCreationCoordination(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.operations.partition_creation":
            raise ModuleNotFoundError("creation coordination dependency forbidden")

sys.meta_path.insert(0, NoCreationCoordination())
core = importlib.import_module("hmcpctl.operations.lpar.core")
assert callable(core.delete_lpar)
assert callable(core.rename_lpar)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_partition_path_document_owner_is_independent_and_shared():
    script = """
import importlib.abc
import sys

class NoClientLayer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hmcpctl.client" or fullname.startswith("hmcpctl.client."):
            raise ModuleNotFoundError(fullname)

finder = NoClientLayer()
sys.meta_path.insert(0, finder)
from hmcpctl.documents.lpar import exact_partition_path
sys.meta_path.remove(finder)
from hmcpctl.client import client_lpars
from hmcpctl.operations.lpar import boot_order
assert client_lpars.exact_partition_path is exact_partition_path
assert boot_order.exact_partition_path is exact_partition_path
assert not hasattr(client_lpars, "_exact_partition_path")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_storage_owners_have_one_way_dependencies():
    script = """
import importlib.abc
import sys

blocked = {
    "hmcpctl.operations.storage.optical",
    "hmcpctl.operations.storage.iso_upload",
}
class NoReverseStorageDependency(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError(fullname)

finder = NoReverseStorageDependency()
sys.meta_path.insert(0, finder)
from hmcpctl.operations.storage import resources
assert not hasattr(resources, "upload_iso")
assert not hasattr(resources, "mount_optical_media")
blocked.remove("hmcpctl.operations.storage.optical")
from hmcpctl.operations.storage import optical
blocked.clear()
from hmcpctl.operations.storage import iso_upload
sys.meta_path.remove(finder)
assert optical.StorageOpticalResult.__bases__ == (resources.StorageMutationResult,)
assert iso_upload.StorageUploadResult.__bases__ == (resources.StorageMutationResult,)
assert optical._optional_number is resources._optional_number
assert iso_upload.logger.name == "hmcpctl.operations.storage.resources"
assert optical.mount_optical_media.__module__ == optical.__name__
assert iso_upload.upload_iso.__module__ == iso_upload.__name__
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
