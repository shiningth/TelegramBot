from __future__ import annotations

import csv
from pathlib import Path

from telegram_bot.shared.csv_prepare import first_link, prepare_csv

HEADER = ["code", "magnets", "magnet_hash", "source_site"]
ED2K_A = "ed2k://|file|98T@ABP-975-U_LTX.mp4|1234|0123456789ABCDEF0123456789ABCDEF|/"
ED2K_B = "ed2k://|file|98T@IPX-001_LTX.mp4|5678|FEDCBA9876543210FEDCBA9876543210|/"
ED2K_C = "ed2k://|file|98T@SSIS-002_LTX.mp4|9012|AAAABBBBCCCCDDDDEEEEFFFF00001111|/"


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_normalizes_placeholder_hash(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "in.csv", [["ABP-975", f'["{ED2K_A}"]', "ltx-2085", "ltx"]])
    result = prepare_csv(path)

    assert result.rows == 1
    assert result.normalized == 1
    assert result.path != path
    assert read_rows(result.path)[0]["magnet_hash"] == "0123456789abcdef0123456789abcdef"
    # 原文件不动
    assert read_rows(path)[0]["magnet_hash"] == "ltx-2085"
    result.path.unlink()


def test_clean_csv_is_uploaded_as_is(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "in.csv",
        [["ABP-975", f'["{ED2K_A}"]', "0123456789abcdef0123456789abcdef", "ltx"]],
    )
    result = prepare_csv(path)

    assert result.normalized == 0
    assert result.path == path
    assert result.rows == 1


def test_existing_rows_are_removed_and_counted(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "in.csv",
        [
            ["ABP-975", f'["{ED2K_A}"]', "", "ltx"],
            ["IPX-001", f'["{ED2K_B}"]', "", "ltx"],
        ],
    )
    seen: list[tuple[str, str, str]] = []

    def lookup(code: str, ed2k: str, source_site: str = "") -> bool:
        seen.append((code, ed2k, source_site))
        return code == "ABP-975"

    result = prepare_csv(path, lookup=lookup)

    assert result.checked is True
    assert result.existing == 1
    assert result.existing_codes == ["ABP-975"]
    assert [row["code"] for row in read_rows(result.path)] == ["IPX-001"]
    # 每行只查一次，且带上 CSV 里的 source_site
    assert len(seen) == 2 and seen[0][2] == "ltx"
    result.path.unlink()


def test_rows_without_code_are_kept_without_lookup(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "in.csv", [["", f'["{ED2K_A}"]', "", "ltx"]])
    calls: list[str] = []

    result = prepare_csv(path, lookup=lambda code, ed2k, site="": calls.append(code) or False)

    assert calls == []
    assert result.existing == 0
    assert result.rows == 1


def test_in_file_duplicates_keep_first(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "in.csv",
        [
            ["ABP-975", f'["{ED2K_A}"]', "", "ltx"],
            ["ABP-975", f'["{ED2K_A}"]', "", "ltx"],
        ],
    )
    result = prepare_csv(path)

    assert result.duplicated == 1
    assert len(read_rows(result.path)) == 1


def test_precheck_failure_falls_back_to_import(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "in.csv", [["ABP-975", f'["{ED2K_A}"]', "", "ltx"]])

    def boom(code: str, ed2k: str, source_site: str = "") -> bool:
        raise RuntimeError("momo 查询失败")

    result = prepare_csv(path, lookup=boom)

    assert result.existing == 0
    assert result.rows == 1
    assert result.skipped_reason == "预检失败"
    assert result.checked is False
    # 哈希仍被规范化，只是没做预检
    assert read_rows(result.path)[0]["magnet_hash"] == "0123456789abcdef0123456789abcdef"


def test_every_row_known_gives_empty_file(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "in.csv",
        [["ABP-975", f'["{ED2K_A}"]', "", "ltx"], ["IPX-001", f'["{ED2K_B}"]', "", "ltx"]],
    )
    result = prepare_csv(path, lookup=lambda code, ed2k, site="": True)

    assert result.rows == 0
    assert result.total == 2
    assert result.existing == 2
    assert read_rows(result.path) == []


def test_csv_without_magnets_column_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "plain.csv"
    path.write_text("id,title\n1,hello\n2,world\n", encoding="utf-8")

    result = prepare_csv(path)

    assert result.path == path
    assert result.rows == 2
    assert result.skipped_reason == "没有 magnets 列"


def test_gbk_csv_is_readable(tmp_path: Path) -> None:
    path = tmp_path / "gbk.csv"
    path.write_bytes(
        ("code,magnets\nABP-975,\"%s\"\n" % ED2K_A).encode("gb18030")
    )
    result = prepare_csv(path)

    assert result.rows == 1
    assert result.normalized == 1


def test_first_link_handles_json_text_and_empty() -> None:
    assert first_link(f'["{ED2K_A}"]') == ED2K_A
    assert first_link(ED2K_B) == ED2K_B
    assert first_link(f"{ED2K_A}\n{ED2K_B}") == ED2K_A
    assert first_link('[{"url": "magnet:?xt=urn:btih:ABC"}]') == "magnet:?xt=urn:btih:ABC"
    assert first_link("") == ""
    assert first_link("[]") == ""

def test_progress_callback_reports_total(tmp_path: Path) -> None:
    rows = [
        [f"ABP-{i:03d}", f'["ed2k://|file|98T@ABP-{i:03d}_LTX.mp4|10{i}|{i:032X}|/"]', "", "ltx"]
        for i in range(1, 41)
    ]
    path = write_csv(tmp_path / "many.csv", rows)
    seen: list[tuple[int, int]] = []

    result = prepare_csv(
        path,
        lookup=lambda code, ed2k, site="": False,
        workers=4,
        progress=lambda done, total: seen.append((done, total)),
    )

    assert result.rows == 40
    assert seen and seen[-1] == (40, 40)
    assert all(total == 40 for _, total in seen)
    # 进度必须单调不减
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


def test_parallel_lookup_drops_existing_rows(tmp_path: Path) -> None:
    rows = [
        [f"ABP-{i:03d}", f'["ed2k://|file|98T@ABP-{i:03d}_LTX.mp4|10{i}|{i:032X}|/"]', "", "ltx"]
        for i in range(1, 11)
    ]
    path = write_csv(tmp_path / "par.csv", rows)
    checked: list[str] = []
    import threading

    lock = threading.Lock()

    def lookup(code: str, ed2k: str, site: str = "") -> bool:
        with lock:
            checked.append(code)
        return code in {"ABP-003", "ABP-007"}

    result = prepare_csv(path, lookup=lookup, workers=4)

    assert result.existing == 2
    assert result.rows == 8
    assert sorted(checked) == sorted(f"ABP-{i:03d}" for i in range(1, 11))
    codes = [row["code"] for row in read_rows(result.path)]
    assert "ABP-003" not in codes and "ABP-007" not in codes
