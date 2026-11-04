#!/usr/bin/env python3
"""The mcu logo (mcu: multiple compute unit), a lava cube: three black facets
carved with concentric arcs in relief (each facet's arcs turn around its own
corner), lifted a few pixels off a cube of lava that glows along the three
edges between them; seen through a perspective camera. Writes mcu.svg here
and the dashboard's copy (dashboard/public/logo.svg: its header, its login
page and its favicon).

The drafts that led to it (01-27) are in git history: `git log -- brand/logo`.

pt3(x, y, z) projects a point of the unit cube: isometric by default, through
the perspective camera when CAMERA is set."""

import math
from pathlib import Path

HERE = Path(__file__).parent
C30, S30 = math.cos(math.pi / 6), 0.5
LIFT = 0.11                                         # how far the facets lift off the cube
BLACK = {"top": "#24201d", "left": "#141211", "right": "#0a0909"}
LAVA = ("#ffd36b", "#f26a1e", "#c8320e")            # the cube's top, left and right sides
K = dict(cx=100, cy=102, s=76)


CAMERA = None   # None: isometric; else a perspective camera, see perspective()


def pt3(x, y, z, cx, cy, s):
    if CAMERA:
        return CAMERA(x, y, z)
    return (cx + (x - y) * C30 * s, cy + (x + y) * S30 * s - z * s)


def perspective(az=52, el=30, dist=4.2, focal=400, centre=(0.55, 0.55, 0.5)):
    """a camera that looks at the cube from az degrees around it (45 is
    square on the edge, more shows the left face) and el degrees above,
    from dist cube sides away: lines converge, as in a photo"""
    a, e = math.radians(az), math.radians(el)
    eye = [centre[0] + dist * math.cos(e) * math.cos(a), centre[1] + dist * math.cos(e) * math.sin(a),
           centre[2] + dist * math.sin(e)]
    f = [c - o for c, o in zip(centre, eye)]
    n = math.sqrt(sum(v * v for v in f))
    f = [v / n for v in f]
    r = [-f[1], f[0], 0.0]                                  # up x forward: right on screen
    n = math.sqrt(sum(v * v for v in r))
    r = [v / n for v in r]
    u = [f[1] * r[2] - f[2] * r[1], f[2] * r[0] - f[0] * r[2], f[0] * r[1] - f[1] * r[0]]   # forward x right: up

    def cam(x, y, z):
        d = [x - eye[0], y - eye[1], z - eye[2]]
        X, Y, Z = (sum(a * b for a, b in zip(d, w)) for w in (r, u, f))
        return (focal * X / Z, -focal * Y / Z)
    return cam


def poly(points, **attrs):
    d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in points)
    a = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f'<path d="{d}" {a}/>'


def face(points, color):
    """a filled quad; a hairline of its own colour closes anti-aliasing seams"""
    pts = [pt3(*v, **K) for v in points]
    return poly(pts + [pts[0]], fill=color, stroke=color, stroke_width=0.5, stroke_linejoin="round")


def logo():
    lift, hi = LIFT, 1 + LIFT
    # the lava cube
    out = face([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], LAVA[0])
    out += face([(0, 1, 1), (1, 1, 1), (1, 1, 0), (0, 1, 0)], LAVA[1])
    out += face([(1, 1, 1), (1, 0, 1), (1, 0, 0), (1, 1, 0)], LAVA[2])
    # the glow along the edges between the facets, kept inside the outline
    edges = "".join(poly([pt3(*a, **K), pt3(*b, **K)], fill="none")
                    for a, b in [((1, 1, 1), (0, 1, 1)), ((1, 1, 1), (1, 0, 1)), ((1, 1, 1), (1, 1, 0))])
    hexa = [pt3(*v, **K) for v in [(0, 0, hi), (hi, 0, hi), (hi, 0, 0), (hi, hi, 0), (0, hi, 0), (0, hi, hi)]]
    sil = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in hexa) + " Z"
    out += ('<defs><filter id="glow" x="-20%" y="-20%" width="140%" height="140%">'
            f'<feGaussianBlur stdDeviation="6"/></filter><clipPath id="sil"><path d="{sil}"/></clipPath></defs>')
    out += (f'<g clip-path="url(#sil)"><g stroke="#ff8a1e" stroke-width="{lift * K["s"] * 2.2:.1f}" opacity="0.85" '
            f'filter="url(#glow)">{edges}</g><g stroke="#fff0a8" stroke-width="2.6" opacity="0.95">{edges}</g></g>')
    # the black facets, lifted off their faces
    out += face([(0, 0, hi), (1, 0, hi), (1, 1, hi), (0, 1, hi)], BLACK["top"])
    out += face([(0, hi, 1), (1, hi, 1), (1, hi, 0), (0, hi, 0)], BLACK["left"])
    out += face([(hi, 1, 1), (hi, 0, 1), (hi, 0, 0), (hi, 1, 0)], BLACK["right"])
    out += relief_arcs(hi)
    # a square frame around the cube, tight enough for a favicon
    box = "10 12 180 180"
    if CAMERA:
        # frame the cube: a square around its projected corners
        pts = [pt3(x, y, z, **K) for x in (0, hi) for y in (0, hi) for z in (0, hi)]
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        side = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.08
        mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        box = f"{mx - side / 2:.1f} {my - side / 2:.1f} {side:.1f} {side:.1f}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" width="180" height="180">'
            f"<title>mcu</title>{out}</svg>\n")


# the carving, drawn in a facet's own square (u right, v down, 0..1)
def arc(cx, cy, r, a0, a1, steps=40):
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / steps), cy + r * math.sin(a0 + (a1 - a0) * i / steps))
            for i in range(steps + 1)]


# concentric arcs, each facet centred on its own corner (its own direction)
ARC_CENTRES = {"top": (0, 0), "left": (0, 1), "right": (1, 1)}


def relief_arcs(hi, step=0.19, width=0.066):
    """concentric arcs carved in each facet: every groove is a light band
    and, just outside it, a band of shadow, so the facets seem to have
    relief; each facet's arcs turn around a different corner. The bands are
    drawn in the facet's own plane, so a perspective thins them like the
    facet itself"""
    to3 = {"top": lambda u, v: (u, v, hi), "left": lambda u, v: (u, hi, 1 - v),
           "right": lambda u, v: (hi, 1 - u, 1 - v)}
    out = ""
    for name, f in to3.items():
        cx, cy = ARC_CENTRES[name]
        a0 = math.atan2(0.5 - cy, 0.5 - cx) - math.pi / 4       # the quarter facing the facet
        a1 = a0 + math.pi / 2
        quad = [pt3(*f(u, v), **K) for u, v in [(0, 0), (1, 0), (1, 1), (0, 1)]]
        clip = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in quad) + " Z"

        def band(r0, r1):
            ring = arc(cx, cy, r1, a0 - 0.2, a1 + 0.2, 60) + arc(cx, cy, max(r0, 0.001), a1 + 0.2, a0 - 0.2, 60)
            return poly([pt3(*f(u, v), **K) for u, v in ring] + [pt3(*f(*ring[0]), **K)], stroke="none")
        light, shade = "", ""
        r = step
        while r < 1.5:
            shade += band(r + width * 0.2, r + width * 0.95)
            light += band(r - width / 2, r + width / 2)
            r += step
        out += (f'<defs><clipPath id="r-{name}"><path d="{clip}"/></clipPath></defs>'
                f'<g clip-path="url(#r-{name})"><g fill="#000000" opacity="0.85">{shade}</g>'
                f'<g fill="#7d766c" opacity="0.9">{light}</g></g>')
    return out

def main():
    global CAMERA
    CAMERA = perspective()
    svg = logo()
    (HERE / "mcu.svg").write_text(svg)
    public = HERE.parent.parent / "dashboard" / "public"
    public.mkdir(exist_ok=True)
    (public / "logo.svg").write_text(svg)

if __name__ == "__main__":
    main()
