import io
import tarfile

import pytest

from scripts.install_linux_offline import safe_extract


def test_interpreter_archive_extracts_inside_owned_directory(tmp_path):
    archive = tmp_path / "python.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo("python/bin/example")
        item.size = 4
        stream.addfile(item, io.BytesIO(b"test"))
    destination = tmp_path / "interpreter"
    safe_extract(archive, destination)
    assert (destination / "python/bin/example").read_bytes() == b"test"
    with pytest.raises(FileExistsError):
        safe_extract(archive, destination)


@pytest.mark.parametrize("kind", ["parent_path", "symlink", "hardlink", "device"])
def test_interpreter_archive_rejects_escaping_or_special_members(tmp_path, kind):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo("../outside" if kind == "parent_path" else "python/link")
        if kind in {"symlink", "hardlink"}:
            item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            item.linkname = "../../outside"
        elif kind == "device":
            item.type = tarfile.CHRTYPE
        stream.addfile(item)
    with pytest.raises(ValueError):
        safe_extract(archive, tmp_path / "interpreter")
    assert not (tmp_path / "outside").exists()
