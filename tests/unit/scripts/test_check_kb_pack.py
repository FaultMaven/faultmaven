"""Tests for scripts/check_kb_pack.py — the gate that keeps the vendored KB pack honest.

The gate used to hash only the runbook markdown and check the vector dimension,
so a chunk whose text no longer matched the vector beside it passed (#1286).
Each case below breaks one text<->vector pairing in a small synthetic pack, its
hashes written by the producer's recipe, and asserts the gate names it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from faultmaven.bootstrap.kb_pack import KbPack

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_kb_pack.py"
#: relpath -> (runbook id, chunk texts). Two runbooks, so the second one's
#: chunk_index differs from its vector_row.
RUNBOOKS = {
    "global/db/alpha.md": ("alpha", ("alpha one", "alpha two")),
    "global/net/beta.md": ("beta", ("beta one", "beta two", "beta three")),
}
BETA_1_ROW = 3  # the row of "global/net/beta.md chunk 1"


@pytest.fixture
def check(capsys):
    """Run the gate on a pack and its sources; return (exit code, stdout)."""
    spec = importlib.util.spec_from_file_location("check_kb_pack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _check(pack=None, src=None):
        if pack is None:
            return module.main(None, src), capsys.readouterr().out
        code = module.main(pack, src or pack.parent / "src")
        return code, capsys.readouterr().out

    return _check


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vectors(*, seed: int = 0, ulp_row: int | None = None) -> np.ndarray:
    """Seeded vectors; ``ulp_row`` moves ONE row by one ulp (same text, other bits)."""
    v = np.random.default_rng(seed).standard_normal((5, 1024)).astype(np.float32)
    if ulp_row is not None:
        v[ulp_row] = np.nextafter(v[ulp_row], np.float32(np.inf))
    return v


def _build(root: Path, vectors: np.ndarray) -> Path:
    """Write a pack as the producer does, plus its runbook sources; return the pack."""
    pack = root / "pack"
    pack.mkdir(parents=True)
    np.savez(pack / "vectors.npz", vectors=vectors)
    runbooks, row = [], 0
    for relpath, (rid, texts) in RUNBOOKS.items():
        md = f"---\nid: {rid}\ntitle: {rid}\nscope: global\n---\n# {rid}\n"
        for base in (pack / "runbooks", root / "src"):
            (base / relpath).parent.mkdir(parents=True, exist_ok=True)
            (base / relpath).write_text(md, encoding="utf-8")
        chunks = []
        for ci, text in enumerate(texts):
            ts = _sha(text.encode("utf-8"))
            vec = np.ascontiguousarray(vectors[row], dtype="<f4").tobytes()
            chunks.append(
                dict(
                    chunk_index=ci,
                    vector_row=row,
                    text=text,
                    text_sha256=ts,
                    pair_sha256=_sha(bytes.fromhex(ts) + vec),
                )
            )
            row += 1
        runbooks.append(
            dict(
                item_id=f"kb_{rid}",
                content_hash=_sha(md.encode("utf-8")),
                title=rid,
                scope="global",
                relpath=relpath,
                tags=[],
                chunks=chunks,
            )
        )
    manifest = dict(
        pack_format=1,
        version="test",
        model="BAAI/bge-m3",
        dim=1024,
        total_chunks=row,
        vectors_sha256=_sha((pack / "vectors.npz").read_bytes()),
        runbooks=runbooks,
    )
    (pack / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _edit(pack: Path, edit) -> Path:
    """Apply ``edit`` to the pack's parsed manifest and write it back."""
    path = pack / "pack.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def test_unchanged_pack_passes(check, tmp_path):
    code, out = check(_build(tmp_path, _vectors()))
    assert code == 0, out
    assert "OK: KB pack valid" in out


@pytest.mark.parametrize("recompute_text_sha", [False, True])
def test_chunk_text_edited_with_its_vector_left_fails(
    check, tmp_path, recompute_text_sha
):
    def edit(manifest):
        chunk = manifest["runbooks"][1]["chunks"][1]
        chunk["text"] = "beta two, reworded"
        if recompute_text_sha:  # the text hash alone is not enough: the pair binds
            chunk["text_sha256"] = _sha(chunk["text"].encode("utf-8"))

    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    assert "global/net/beta.md chunk 1: " in out
    assert "rebuild the pack" in out and "never edit pack.json" in out


def test_two_vector_rows_swapped_fails(check, tmp_path):
    def edit(manifest):
        a, b = (
            manifest["runbooks"][0]["chunks"][1],
            manifest["runbooks"][1]["chunks"][0],
        )
        a["vector_row"], b["vector_row"] = b["vector_row"], a["vector_row"]

    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    assert "global/db/alpha.md chunk 1: pair_sha256" in out
    assert "global/net/beta.md chunk 0: pair_sha256" in out
    assert "vector_row: " in out


@pytest.mark.parametrize("rb, ci", [(0, 1), (1, 2)], ids=["middle", "last"])
def test_chunk_dropped_fails(check, tmp_path, rb, ci):
    edit = lambda manifest: manifest["runbooks"][rb]["chunks"].pop(ci)  # noqa: E731
    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    assert "vector_row: " in out


def test_vectors_replaced_by_same_shape_fails(check, tmp_path):
    pack = _build(tmp_path, _vectors())
    np.savez(pack / "vectors.npz", vectors=_vectors(seed=1))
    code, out = check(pack)
    assert code == 1
    assert "vectors_sha256 != " in out
    assert "global/db/alpha.md chunk 0: pair_sha256" in out


@pytest.mark.parametrize("vectors_sha_from", ["manifest", "vectors"])
@pytest.mark.parametrize("manifest_from", ["a", "b"])
def test_merge_of_two_builds_names_the_one_chunk(
    check, tmp_path, manifest_from, vectors_sha_from
):
    """Texts + pair hashes from one build, vectors.npz from another of the same texts.

    B differs from A in ONE row only, so a check bound to the wrong row cannot
    pass; with ``vectors_sha256`` taken from the vectors' build, only the pair
    hash catches it.
    """
    a = _build(tmp_path / "a", _vectors())
    b = _build(tmp_path / "b", _vectors(ulp_row=BETA_1_ROW))
    pack, other = (a, b) if manifest_from == "a" else (b, a)
    shutil.copyfile(other / "vectors.npz", pack / "vectors.npz")
    if vectors_sha_from == "vectors":
        sha = json.loads((other / "pack.json").read_text())["vectors_sha256"]
        _edit(pack, lambda manifest: manifest.update(vectors_sha256=sha))
    code, out = check(pack)
    assert code == 1
    assert out.count("pair_sha256 != ") == 1, out
    expected = f"beta.md chunk 1: pair_sha256 != its text + vectors[{BETA_1_ROW}]"
    assert expected in out
    assert ("vectors_sha256 != " in out) is (vectors_sha_from == "manifest")


@pytest.mark.parametrize("field", ["text_sha256", "pair_sha256"])
def test_chunk_without_the_hashes_fails_naming_the_rebuild(check, tmp_path, field):
    edit = lambda manifest: manifest["runbooks"][1]["chunks"][1].pop(
        field
    )  # noqa: E731
    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    expected = (
        "rebuild with a kb-toolkit build that records text_sha256 and pair_sha256"
    )
    assert f"global/net/beta.md chunk 1: no {field} — {expected}" in out


def test_total_chunks_not_the_vector_rows_fails(check, tmp_path):
    pack = _edit(_build(tmp_path, _vectors()), lambda m: m.update(total_chunks=4))
    code, out = check(pack)
    assert code == 1
    assert "total_chunks != the 5 rows of vectors.npz" in out


@pytest.mark.parametrize(
    "drop, expected",
    [
        (lambda m: m.pop("runbooks"), "pack contains 0 runbooks"),
        (lambda m: m["runbooks"][0].pop("chunks"), "global/db/alpha.md: 0 chunks"),
    ],
    ids=["runbooks", "chunks"],
)
def test_absent_runbooks_or_chunks_are_reported_not_raised(
    check, tmp_path, drop, expected
):
    """The loader reads both as empty when absent; the gate must report, not crash."""
    code, out = check(_edit(_build(tmp_path, _vectors()), drop))
    assert code == 1
    assert expected in out


@pytest.mark.parametrize("text", [None, 42, "\ud800"], ids=["null", "int", "surrogate"])
def test_malformed_chunk_text_is_reported_not_raised(check, tmp_path, text):
    edit = lambda m: m["runbooks"][1]["chunks"][1].update(text=text)  # noqa: E731
    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    assert "global/net/beta.md chunk 1: text is not a UTF-8 string" in out


@pytest.mark.parametrize(
    "indices", [[1, 0, 2], [0, 0, 2]], ids=["swapped", "duplicated"]
)
def test_chunk_index_not_in_order_fails(check, tmp_path, indices):
    """Startup refuses such a runbook (``kb_init``); the gate names it (#2245)."""

    def edit(manifest):
        for chunk, ci in zip(manifest["runbooks"][1]["chunks"], indices):
            chunk["chunk_index"] = ci

    code, out = check(_edit(_build(tmp_path, _vectors()), edit))
    assert code == 1
    assert f"global/net/beta.md: chunk_index sequence {indices} is not 0..2" in out


def test_pack_runbook_without_a_source_fails(check, tmp_path):
    """A source deleted without a rebuild leaves its runbook in the pack (#2246)."""
    pack = _build(tmp_path, _vectors())
    (tmp_path / "src" / "global" / "net" / "beta.md").unlink()
    code, out = check(pack)
    assert code == 1
    assert (
        "pack runbook not in sources (deleted, or edited without a rebuild): "
        "global/net/beta.md"
    ) in out
    assert "global/db/alpha.md" not in out


def test_sources_holding_only_a_readme_fail(check, tmp_path):
    """A README (no frontmatter) is not a runbook, so it matches no pack runbook."""
    pack = _build(tmp_path, _vectors())
    shutil.rmtree(tmp_path / "src")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "README.md").write_text("# Runbooks\n", encoding="utf-8")
    code, out = check(pack)
    assert code == 1
    for relpath in RUNBOOKS:
        assert (
            f"pack runbook not in sources (deleted, or edited without a rebuild): {relpath}"
            in out
        )


def test_missing_sources_directory_fails(check, tmp_path):
    pack = _build(tmp_path, _vectors())
    src = tmp_path / "no-such-dir"
    code, out = check(pack, src)
    assert code == 1
    assert f"runbook sources directory not found: {src}" in out


def test_big_endian_float32_vectors_pass(check, tmp_path):
    """``>f4`` is a float32 in the other byte order, and a valid pack."""
    code, out = check(_build(tmp_path, _vectors().astype(">f4")))
    assert code == 0, out


def test_vendored_pack_less_one_source_fails(check, tmp_path):
    """The issue's reproduction: the vendored pack against its sources less one."""
    src = tmp_path / "src"
    shutil.copytree(SCRIPT.parents[1] / "resources" / "knowledge" / "runbooks", src)
    (src / "application" / "argocd-sync-failure.md").unlink()
    code, out = check(src=src)
    assert code == 1
    assert (
        "pack runbook not in sources (deleted, or edited without a rebuild): "
        "global/application/argocd-sync-failure.md"
    ) in out


def _vectors_as(pack: Path, vectors: np.ndarray) -> Path:
    np.savez(pack / "vectors.npz", vectors=vectors)
    return pack


@pytest.mark.parametrize(
    "corrupt, expected",
    [
        (
            lambda p: _vectors_as(p, np.full((5, 1024), "a")),
            "vectors dtype is <U1, not float32",
        ),
        (
            lambda p: _vectors_as(p, _vectors().astype(np.float64)),
            "vectors dtype is float64, not float32",
        ),
        (
            lambda p: _vectors_as(p, np.zeros((5, 1024), dtype=np.int32)),
            "vectors dtype is int32, not float32",
        ),
        (
            lambda p: _vectors_as(p, np.zeros((5, 1024), dtype=bool)),
            "vectors dtype is bool, not float32",
        ),
        (
            lambda p: _vectors_as(p, _vectors().astype(np.float16)),
            "vectors dtype is float16, not float32",
        ),
        (
            lambda p: _edit(p, lambda m: m["runbooks"][1].update(content_hash=[])),
            "runbook 'kb_beta' has a content_hash of type list",
        ),
        (
            lambda p: _edit(p, lambda m: m["runbooks"][1].update(content_hash={})),
            "runbook 'kb_beta' has a content_hash of type dict",
        ),
    ],
    ids=[
        "str-vectors",
        "float64-vectors",
        "int-vectors",
        "bool-vectors",
        "float16-vectors",
        "list-hash",
        "dict-hash",
    ],
)
def test_wrongly_typed_value_is_refused_by_the_loader(
    check, tmp_path, caplog, corrupt, expected
):
    """``KbPack.load`` refuses the pack, naming the value; the gate exits 1 (#2248)."""
    pack = corrupt(_build(tmp_path, _vectors()))
    with caplog.at_level("ERROR", logger="faultmaven.bootstrap.kb_pack"):
        assert KbPack.load(pack) is None
        code, out = check(pack)
    assert code == 1
    assert "no valid KB pack" in out
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 2 and all(expected in e for e in errors), errors


def test_vendored_pack_passes(check):
    """Second control: the pack this repo ships, against its runbook sources."""
    code, out = check()
    assert code == 0, out
