from datetime import timedelta

import pytest

from ashare_lab.contracts import ContractError, as_of
from ashare_lab.storage import query_as_of, read_dataset, write_dataset
from ashare_lab.synthetic import at, fixture, sessions


def test_roundtrip_and_duckdb_before_at_after_revision(tmp_path):
    tables = fixture()
    path = write_dataset(tmp_path, "synthetic-test", tables, data_kind="synthetic")
    restored = read_dataset(path)
    assert set(restored) == set(tables)
    for name, table in tables.items():
        assert restored[name].equals(table)
        for delta in (-1, 0, 1):
            cutoff = at(sessions()[2], 17) + timedelta(microseconds=delta)
            expected = as_of(name, table, cutoff).to_pylist()
            actual = query_as_of(path, name, cutoff).to_pylist()
            assert sorted(map(str, actual)) == sorted(map(str, expected))
    with pytest.raises(FileExistsError):
        write_dataset(tmp_path, "synthetic-test", tables, data_kind="synthetic")


def test_parquet_tampering_fails_closed(tmp_path):
    path = write_dataset(tmp_path, "synthetic-test", fixture(), data_kind="synthetic")
    with (path / "bars.parquet").open("ab") as file:
        file.write(b"corruption")
    with pytest.raises(ContractError, match="checksum"):
        read_dataset(path)


def test_namespace_and_path_traversal_rejected(tmp_path):
    with pytest.raises(ContractError, match="invalid dataset"):
        write_dataset(tmp_path, "../real", fixture(), data_kind="synthetic")
    with pytest.raises(ContractError, match="cannot be mixed"):
        write_dataset(tmp_path, "real-v1", fixture(), data_kind="real")
