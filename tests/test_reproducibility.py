import hashlib

from ashare_lab.storage import write_dataset
from ashare_lab.synthetic import fixture


def test_seeded_input_and_parquet_bytes_are_reproducible(tmp_path):
    first = write_dataset(tmp_path / "a", "synthetic-test", fixture(17), data_kind="synthetic")
    second = write_dataset(tmp_path / "b", "synthetic-test", fixture(17), data_kind="synthetic")
    for path in first.glob("*.parquet"):
        assert (
            hashlib.sha256(path.read_bytes()).digest()
            == hashlib.sha256((second / path.name).read_bytes()).digest()
        )
    assert not fixture(17)["bars"].equals(fixture(18)["bars"])
