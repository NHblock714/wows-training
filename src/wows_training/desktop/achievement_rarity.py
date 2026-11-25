"""Shared medal colours for desktop achievement views."""
from typing import NamedTuple


class RarityStyle(NamedTuple):
    label: str
    accent: str  # Medal metal, or lettering on navy.
    ink: str     # Lettering and keylines on paper.
    rank: int    # Ascending metal grade, shared by ordering and hover depth.
    english: str  # Display lettering on the gallery certificate.


_STYLES = {
    style.label: style for style in (
        RarityStyle("神话", "#FF6971", "#A22237", 5, "MYTHIC"),
        RarityStyle("传奇", "#F6BF52", "#825509", 4, "LEGENDARY"),
        RarityStyle("史诗", "#C39AFF", "#67429A", 3, "EPIC"),
        RarityStyle("稀有", "#76B9FF", "#265E96", 2, "RARE"),
        RarityStyle("罕见", "#76D8A3", "#246646", 1, "UNCOMMON"),
        RarityStyle("普通", "#F6FAFF", "#4A5B6C", 0, "COMMON"),
    )
}


def rarity_style(label: str | None) -> RarityStyle:
    return _STYLES.get(label, _STYLES["普通"])
