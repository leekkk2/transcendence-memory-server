import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import { useTranslation } from 'react-i18next';
import { Maximize2 } from 'lucide-react';
import { GraphNode, GraphSnapshot } from '../lib/documents';

export function GraphCanvas({ graph, onSelect }: { graph: GraphSnapshot; onSelect: (node: GraphNode) => void }) {
  const { t } = useTranslation();
  const canvas = useRef<HTMLDivElement>(null);
  const instance = useRef<cytoscape.Core>();
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  useEffect(() => {
    if (!canvas.current) return;
    const css = getComputedStyle(document.documentElement);
    const color = (name: string) => css.getPropertyValue(name).trim();
    const palette = ['#22d3ee', '#a78bfa', '#3fb950', '#fbbf24', '#fb7185', '#38bdf8'];
    const types = [...new Set(graph.nodes.map(node => node.type))].sort();
    const cy = cytoscape({
      container: canvas.current,
      elements: [
        ...graph.nodes.map(node => ({ data: { id: node.id, label: node.label, color: palette[types.indexOf(node.type) % palette.length] } })),
        ...graph.edges.map(edge => ({ data: { id: 'tm-view-' + edge.id, source: edge.source, target: edge.target } })),
      ],
      style: [
        { selector: 'node', style: { 'background-color': 'data(color)', label: 'data(label)', color: color('--text'), 'font-size': 11, 'text-valign': 'bottom', 'text-margin-y': 7, 'text-max-width': '110px', 'text-wrap': 'ellipsis', width: 20, height: 20, 'border-width': 2, 'border-color': color('--bg-elev') } },
        { selector: 'edge', style: { 'line-color': color('--text-dim'), opacity: 0.35, width: 1.4, 'curve-style': 'bezier' } },
        { selector: 'node:selected', style: { 'border-width': 4, 'border-color': color('--accent'), width: 28, height: 28 } },
      ],
      layout: { name: 'cose', animate: false, padding: 35, randomize: false, nodeRepulsion: () => 13000 },
      minZoom: 0.15, maxZoom: 4, wheelSensitivity: 0.2,
    });
    instance.current = cy;
    cy.on('tap', 'node', event => {
      const node = graph.nodes.find(item => item.id === event.target.id());
      if (node) onSelectRef.current(node);
    });
    const observer = new ResizeObserver(() => cy.resize());
    observer.observe(canvas.current);
    return () => { observer.disconnect(); cy.destroy(); instance.current = undefined; };
  }, [graph]);
  return <div className="relative">
    <div ref={canvas} className="h-96 w-full rounded-lg bg-bg" role="img" aria-label={t('knowledge.graphLabel')} data-testid="graph-canvas" />
    <button className="btn absolute right-3 top-3 flex items-center gap-2" onClick={() => instance.current?.fit(undefined, 35)}><Maximize2 size={14} />{t('knowledge.fit')}</button>
  </div>;
}
