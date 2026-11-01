"""docs/SQL_HIGHLIGHTS.md quotes the project's real SQL. Every code block in it must still appear, word for word, in sql/*.sql."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "SQL_HIGHLIGHTS.md").read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_every_sql_block_in_the_highlights_page_is_copied_from_the_real_files():
    blocks = re.findall(r"```sql\n(.*?)```", DOC, flags=re.S)
    assert len(blocks) >= 6, len(blocks)
    real = _squash("\n".join(f.read_text(encoding="utf-8") for f in sorted((ROOT / "sql").glob("*.sql"))))
    for block in blocks:
        assert _squash(block) in real, f"this block is not in sql/*.sql any more: {block.strip()[:80]!r}"


def test_the_page_quotes_the_files_own_comment_exactly():
    quoted = re.search(r'"(has no result and no stats[^"]*)"', DOC)
    assert quoted, "the page should quote the SQL comment about placeholder rows"
    comment = _squash(re.sub(r"^\s*--", "", (ROOT / "sql" / "features.sql").read_text(encoding="utf-8"), flags=re.M))
    assert _squash(quoted.group(1)) in comment


def test_the_page_makes_each_of_its_five_points_and_the_readme_links_to_it():
    for heading in ("## 1. ", "## 2. ", "## 3. ", "## 4. ", "## 5. "):
        assert heading in DOC, heading
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING" in DOC and "NULLIF" in DOC and "md5" in DOC and "LAG" in DOC
    assert "(docs/SQL_HIGHLIGHTS.md)" in (ROOT / "README.md").read_text(encoding="utf-8")
