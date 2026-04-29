from __future__ import annotations

import graphviz
import streamlit as st

DEFAULT_EDGE_TEXT = """Browser -> Gateway
Gateway -> logo_generator
Gateway -> video_kit
Gateway -> multi_format_exporter
Gateway -> subtitle_studio
Gateway -> storyboard_builder
logo_generator -> generated
video_kit -> generated
multi_format_exporter -> generated
subtitle_studio -> generated
storyboard_builder -> generated
video_kit -> subtitle_studio
video_kit -> multi_format_exporter
storyboard_builder -> video_kit
"""

DOT_ENGINES = ["dot", "neato", "fdp", "sfdp", "circo"]
NODE_SHAPES = ["box", "ellipse", "component", "folder", "tab"]


def parse_edges(raw_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    edges: list[tuple[str, str]] = []
    invalid_lines: list[str] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "->" not in line:
            invalid_lines.append(raw_line)
            continue
        src, dst = [segment.strip() for segment in line.split("->", 1)]
        if not src or not dst:
            invalid_lines.append(raw_line)
            continue
        edges.append((src, dst))

    return edges, invalid_lines


def build_graph(
    edges: list[tuple[str, str]],
    engine: str,
    node_color: str,
    node_shape: str,
    rank_sep: float,
    node_sep: float,
    spring_k: float,
    gap_sep: int,
) -> graphviz.Digraph:
    dot = graphviz.Digraph("workbench_topology", engine=engine)

    if engine == "dot":
        dot.attr(rankdir="LR", ranksep=str(rank_sep), nodesep=str(node_sep))
    else:
        dot.attr(
            overlap="false",
            sep=f"+{gap_sep}",
            K=str(spring_k),
            splines="true",
        )

    dot.attr(
        "node",
        style="filled",
        fillcolor=node_color,
        shape=node_shape,
        fontname="Arial",
    )
    dot.attr("edge", color="#4A4A4A")

    for src, dst in edges:
        dot.edge(src, dst)

    return dot


def main() -> None:
    st.set_page_config(page_title="Workbench Topology", layout="wide")
    st.title("Workbench Topology")
    st.caption("Streamlit + Graphviz editor for x-workbench tool relations.")

    st.sidebar.header("Graph Settings")
    engine = st.sidebar.selectbox("Rendering Engine", DOT_ENGINES, index=0)
    node_color = st.sidebar.color_picker("Node Color", "#E3F2FD")
    node_shape = st.sidebar.selectbox("Node Shape", NODE_SHAPES, index=0)

    st.sidebar.markdown("---")
    st.sidebar.subheader("Layout Tuning")
    if engine == "dot":
        rank_sep = st.sidebar.slider("Layer Spacing (ranksep)", 0.5, 4.0, 1.4)
        node_sep = st.sidebar.slider("Node Spacing (nodesep)", 0.5, 4.0, 1.0)
        spring_k = 1.0
        gap_sep = 30
    else:
        spring_k = st.sidebar.slider("Spring Force (K)", 0.1, 5.0, 1.0)
        gap_sep = st.sidebar.slider("Minimum Gap (sep)", 10, 100, 30)
        rank_sep = 1.4
        node_sep = 1.0

    col_editor, col_output = st.columns([1, 2])

    with col_editor:
        st.subheader("Edge Definitions")
        raw_text = st.text_area(
            "Format: source -> target",
            value=DEFAULT_EDGE_TEXT,
            height=430,
        )

    with col_output:
        st.subheader("Graph Preview")
        edges, invalid_lines = parse_edges(raw_text)

        if invalid_lines:
            st.warning(
                "Some lines are invalid and were skipped:\n"
                + "\n".join(f"- {line}" for line in invalid_lines[:10])
            )

        if not edges:
            st.info("Add at least one valid edge in the editor.")
            return

        graph = build_graph(
            edges=edges,
            engine=engine,
            node_color=node_color,
            node_shape=node_shape,
            rank_sep=rank_sep,
            node_sep=node_sep,
            spring_k=spring_k,
            gap_sep=gap_sep,
        )
        st.graphviz_chart(graph, width="stretch")

        with st.expander("Show DOT Source"):
            st.code(graph.source, language="dot")


if __name__ == "__main__":
    main()
