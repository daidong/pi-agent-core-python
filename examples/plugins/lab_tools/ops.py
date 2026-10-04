from pi_python import tool


@tool
def count_duplicates(rows: list[str]) -> int:
    """Count rows that repeat an earlier row.

    Args:
        rows: The rows, in order.
    """
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        duplicates += row in seen
        seen.add(row)
    return duplicates
