"""
The look of every research figure: type, sizes, colour roles and small helpers.

Shared by every figure module, so a registry or a cluster keeps its colour everywhere.

Type matches the papers' body text: Latin Modern Roman with Computer Modern
mathtext, 7 pt labels and 6 pt ticks at true LNCS widths. Matplotlib can embed
only TrueType outlines cleanly in PDF, so the system's Latin Modern OpenType
files are converted once into the user cache directory (``CACHE_DIR``) (a private, renamed
build; DejaVu Serif when Latin Modern is not installed). Glyphs Latin Modern
lacks fall back to DejaVu Serif per glyph.

Colour follows the GrowGraph brand: ink ``#293241``, blue ``#224777`` and orange
``#E67E22``. The blue enters the categorical palette one step lighter
(``#2a5f9e``), because the brand blue is too dark and too grey to carry
identity as a fill; it stays as the dark end of the sequential ramp.

Colour has four roles, never mixed:

``registry``
    categorical, by registry size, the first seven registries in palette
    order and the rest folded into one neutral "other"; in a scatter (every
    pair of colours on screen together) only the first three keep a hue. The
    two largest closed communities, LOV and OBO, take the brand blue and
    orange.
``status``
    outcome states (fetch failed, parse failed, skipped), only where the
    figure is about outcomes, always with a legend label.
``cluster``
    a four-hue set that separates every pair, for k-means clusters.
``kind``
    any other small categorical set (reuse kinds, fitted distributions).

Reference and median lines are muted ink; text on a filled mark takes the ink
that contrasts with the fill (:func:`ink_on`).
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap, to_rgb

FULL = 4.8  # LNCS text width, inches
HALF = 2.35

# Categorical palette, fixed order: every adjacent pair separates for normal
# and colour-deficient vision; the first three also separate as all pairs.
CATEGORICAL_PALETTE = [
    "#2a5f9e",  # brand blue, one step lighter
    "#1baf7a",  # aqua
    "#E67E22",  # brand orange
    "#6250d6",  # violet
    "#e87ba4",  # magenta
    "#008300",  # green
    "#eda100",  # yellow
    "#e34948",  # red
]
#: Registries beyond this many share the neutral "other".
REGISTRY_SLOTS = 7
#: In a scatter only this many registries keep a hue.
SCATTER_SLOTS = 3
OTHER = "other registries"
#: Four hues that separate as all pairs (validated), for clusters.
CLUSTER_PALETTE = ["#2a5f9e", "#eda100", "#008300", "#e87ba4"]
KIND_PALETTE = CATEGORICAL_PALETTE[:3]
BRAND_BLUE = "#224777"
BRAND_ORANGE = "#E67E22"
#: One hue, light to dark, ending in the brand blue.
SEQUENTIAL_CMAP = LinearSegmentedColormap.from_list("ontometer_seq", ["#f4f6fa", BRAND_BLUE])
#: Blue and orange poles of equal lightness around a neutral midpoint.
DIVERGING_CMAP = LinearSegmentedColormap.from_list(
    "ontometer_div", ["#2a5f9e", "#f2f1ec", "#9a4c0c"]
)

STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

INK = "#293241"
TEXT_MUTED = "#5b6475"
NEUTRAL = "#c3c7cf"
NEUTRAL_LIGHT = "#e6e8ec"
SURFACE = "#ffffff"

_LM_SOURCES = {
    "regular": "lmroman10-regular.otf",
    "bold": "lmroman10-bold.otf",
    "italic": "lmroman10-italic.otf",
    "bolditalic": "lmroman10-bolditalic.otf",
}
_LM_DIRS = [
    Path("/usr/share/texmf/fonts/opentype/public/lm"),
    Path("/usr/share/texlive/texmf-dist/fonts/opentype/public/lm"),
    Path("/usr/local/texlive").glob("*/texmf-dist/fonts/opentype/public/lm"),
]
#: Per-user cache of converted fonts and slow statistics; ``XDG_CACHE_HOME`` moves it.
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ontometer-corpus"
FONT_CACHE = CACHE_DIR / "fonts"
TEXT_FAMILY = "OntoMeter LM Roman"


def _otf_to_ttf(src: Path, dst: Path, family: str) -> None:
    """Convert a CFF OpenType font to TrueType outlines under a private family name."""
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, newTable

    font = TTFont(src)
    order = font.getGlyphOrder()
    glyphs = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyphs)
        glyphs[name].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    font["glyf"] = glyf
    font["loca"] = newTable("loca")
    maxp = font["maxp"]
    maxp.tableVersion = 0x00010000
    for attr in (
        "maxTwilightPoints",
        "maxStorage",
        "maxFunctionDefs",
        "maxInstructionDefs",
        "maxStackElements",
        "maxSizeOfInstructions",
        "maxComponentElements",
    ):
        setattr(maxp, attr, 0)
    maxp.maxZones = 1
    font["head"].glyphDataFormat = 0
    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order
    del font["CFF "]
    if "VORG" in font:
        del font["VORG"]
    font.sfntVersion = "\x00\x01\x00\x00"
    style = font["name"].getDebugName(2) or "Regular"
    for record in list(font["name"].names):
        if record.nameID in (1, 16):
            record.string = family
        elif record.nameID in (3, 4):
            record.string = f"{family} {style}"
        elif record.nameID == 6:
            record.string = f"{family.replace(' ', '')}-{style.replace(' ', '')}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    font.save(dst)


def _text_fonts() -> list[str]:
    """Register the converted Latin Modern faces; the serif fallback list."""
    sources: dict[str, Path] = {}
    for d in _LM_DIRS:
        for directory in [d] if isinstance(d, Path) else list(d):
            for style, name in _LM_SOURCES.items():
                if style not in sources and (directory / name).exists():
                    sources[style] = directory / name
    if "regular" not in sources:
        return ["DejaVu Serif"]
    for style, src in sources.items():
        dst = FONT_CACHE / f"{src.stem}.ttf"
        if not dst.exists():
            _otf_to_ttf(src, dst, TEXT_FAMILY)
        font_manager.fontManager.addfont(str(dst))
    return [TEXT_FAMILY, "DejaVu Serif"]


def style() -> None:
    """Draw off-screen with the rcParams every paper figure uses (scripts, not notebooks)."""
    plt.switch_backend("Agg")
    plt.rcdefaults()
    plt.rcParams.update(
        {
            # A family list, so a glyph Latin Modern lacks falls back per glyph.
            "font.family": _text_fonts(),
            "mathtext.fontset": "cm",
            "axes.formatter.use_mathtext": True,
            "font.size": 7,
            "axes.titlesize": 7,
            "axes.labelsize": 7,
            "xtick.labelsize": 6,
            "ytick.labelsize": 6,
            "legend.fontsize": 6,
            "legend.title_fontsize": 6,
            "legend.frameon": False,
            "legend.handlelength": 1.2,
            "legend.handletextpad": 0.4,
            "legend.borderaxespad": 0.2,
            "axes.titlelocation": "left",
            "axes.titlepad": 3,
            "axes.labelpad": 2,
            "axes.edgecolor": NEUTRAL,
            "axes.labelcolor": INK,
            "axes.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": False,
            "text.color": INK,
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_MUTED,
            "xtick.labelcolor": INK,
            "ytick.labelcolor": INK,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.minor.width": 0.4,
            "ytick.minor.width": 0.4,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "xtick.minor.size": 1.5,
            "ytick.minor.size": 1.5,
            "xtick.major.pad": 2,
            "ytick.major.pad": 2,
            "lines.linewidth": 1.0,
            "patch.linewidth": 0,
            "axes.facecolor": SURFACE,
            "figure.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.02,
        }
    )


# ---------------------------------------------------------------- colour roles
def registry_groups(order: list[str], slots: int = REGISTRY_SLOTS) -> dict[str, str]:
    """Registry -> the group it is drawn as: itself for the first ``slots``, else ``OTHER``."""
    return {r: (r if i < slots else OTHER) for i, r in enumerate(order)}


def registry_colors(order: list[str], slots: int = REGISTRY_SLOTS) -> dict[str, str]:
    """Group -> colour, for the groups :func:`registry_groups` makes (``OTHER`` neutral)."""
    colors = {r: CATEGORICAL_PALETTE[i] for i, r in enumerate(order[:slots])}
    colors[OTHER] = NEUTRAL
    return colors


def cluster_color(c: int) -> str:
    return CLUSTER_PALETTE[c % len(CLUSTER_PALETTE)]


def _luminance(color: str) -> float:
    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def tint(color: str, amount: float) -> str:
    """``color`` mixed toward the surface: 0 keeps the colour, 1 is the surface."""
    surface = to_rgb(SURFACE)
    return matplotlib.colors.to_hex(
        [c + (s - c) * amount for c, s in zip(to_rgb(color), surface, strict=True)]
    )


def ink_on(fill) -> str:
    """White or dark ink, whichever contrasts more with ``fill`` (WCAG contrast ratio)."""
    lum = _luminance(fill if isinstance(fill, str) else matplotlib.colors.to_hex(fill))
    on_white = 1.05 / (lum + 0.05)
    on_dark = (lum + 0.05) / (_luminance(INK) + 0.05)
    return SURFACE if on_white > on_dark else INK


# ---------------------------------------------------------------- helpers
def panel(ax, letter: str | None, title: str) -> None:
    """A panel title, left-aligned, with its letter in bold."""
    label = f"$\\bf{{({letter})}}$ {title}" if letter else title
    ax.set_title(label, loc="left")


def reference_line(ax, value: float, axis: str = "x", **kwargs) -> None:
    """A dotted muted line marking a median or a threshold."""
    line = ax.axvline if axis == "x" else ax.axhline
    line(value, linestyle=(0, (1, 1.5)), linewidth=0.9, color=INK, zorder=3, **kwargs)


def save(fig, out: Path, name: str) -> None:
    """``<name>.pdf`` (no creation date, so reruns are byte-stable) and a 300 dpi PNG."""
    fig.savefig(out / f"{name}.pdf", metadata={"CreationDate": None, "ModDate": None})
    fig.savefig(out / f"{name}.png", dpi=300)


# ---------------------------------------------------------------- number formatting
# LaTeX-ready numbers for ``\statdef`` macros.
def fmt_int(v: float) -> str:
    return f"{int(round(v)):,}".replace(",", "{,}")


def fmt_pct(v: float) -> str:
    return f"{v:.1f}\\%"


def fmt_dec(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}"


def fmt_p(v: float) -> str:
    """A p-value: three decimals, or an upper bound below 0.001."""
    return "< 0.001" if v < 0.001 else f"= {v:.3f}"
