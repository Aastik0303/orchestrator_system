import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react'

// Chart specs produced by the backend data analyst (app/agents/visualization.py)
// and embedded in responses as fenced ```chart JSON blocks.
type Base = { title: string; subtitle?: string }
type Value = number | null

export type ChartSpec =
  | (Base & { type: 'bar'; categories: string[]; values: Value[]; value_label?: string; unit?: string })
  | (Base & { type: 'histogram'; bins: Array<{ start: number; end: number; count: number }>; x_label?: string; y_label?: string })
  | (Base & { type: 'line'; x: string[]; y: Value[]; x_label?: string; y_label?: string })
  | (Base & { type: 'scatter'; points: Array<[number, number]>; x_label?: string; y_label?: string })
  | (Base & { type: 'heatmap'; labels: string[]; matrix: Value[][] })

const isNumberOrNull = (value: unknown) => value === null || (typeof value === 'number' && Number.isFinite(value))
const isStringArray = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string')

export function parseChartSpec(source: string): ChartSpec | null {
  let spec: Record<string, unknown>
  try {
    spec = JSON.parse(source)
  } catch {
    return null
  }
  if (!spec || typeof spec !== 'object' || typeof spec.title !== 'string') return null
  switch (spec.type) {
    case 'bar':
      return isStringArray(spec.categories) && spec.categories.length > 0 && Array.isArray(spec.values) && spec.values.length === spec.categories.length && spec.values.every(isNumberOrNull)
        ? spec as ChartSpec : null
    case 'histogram':
      return Array.isArray(spec.bins) && spec.bins.length > 0 && spec.bins.every(bin => bin && typeof bin === 'object' && ['start', 'end', 'count'].every(key => typeof bin[key] === 'number'))
        ? spec as ChartSpec : null
    case 'line':
      return isStringArray(spec.x) && spec.x.some((_, index) => (spec.y as unknown[])?.[index] != null) && Array.isArray(spec.y) && spec.y.length === spec.x.length && spec.y.every(isNumberOrNull)
        ? spec as ChartSpec : null
    case 'scatter':
      return Array.isArray(spec.points) && spec.points.length > 0 && spec.points.every(point => Array.isArray(point) && point.length === 2 && point.every(item => typeof item === 'number'))
        ? spec as ChartSpec : null
    case 'heatmap':
      return isStringArray(spec.labels) && spec.labels.length > 0 && Array.isArray(spec.matrix) && spec.matrix.length === spec.labels.length
        && spec.matrix.every(row => Array.isArray(row) && row.length === (spec.labels as string[]).length && row.every(isNumberOrNull))
        ? spec as ChartSpec : null
    default:
      return null
  }
}

// ---------------------------------------------------------------- formatting

const compactFormat = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })
const fullFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 })

function formatTick(value: number) {
  return Math.abs(value) >= 10_000 ? compactFormat.format(value) : fullFormat.format(value)
}

function formatValue(value: Value, unit = '') {
  return value === null ? 'n/a' : `${fullFormat.format(value)}${unit}`
}

function truncate(text: string, max: number) {
  return text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1))}…`
}

function niceTicks(min: number, max: number, count = 5) {
  if (min === max) {
    const pad = Math.abs(min) || 1
    min -= pad
    max += pad
  }
  const rough = (max - min) / count
  const power = 10 ** Math.floor(Math.log10(rough))
  const step = [1, 2, 2.5, 5, 10].map(factor => factor * power).find(candidate => candidate >= rough) ?? rough
  const start = Math.floor(min / step) * step
  const end = Math.ceil(max / step) * step
  const ticks: number[] = []
  for (let tick = start; tick <= end + step / 2; tick += step) ticks.push(Number(tick.toPrecision(12)))
  return ticks
}

function scale(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain
  const [r0, r1] = range
  return (value: number) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((value - d0) / (d1 - d0)) * (r1 - r0))
}

/** Rect with 4px rounding on the data end only; square at the baseline. */
function barPath(x: number, y: number, width: number, height: number, end: 'top' | 'bottom' | 'left' | 'right') {
  const r = Math.max(0, Math.min(4, width / 2, height / 2))
  const x2 = x + width
  const y2 = y + height
  switch (end) {
    case 'top':
      return `M${x},${y2}V${y + r}Q${x},${y} ${x + r},${y}H${x2 - r}Q${x2},${y} ${x2},${y + r}V${y2}Z`
    case 'bottom':
      return `M${x},${y}V${y2 - r}Q${x},${y2} ${x + r},${y2}H${x2 - r}Q${x2},${y2} ${x2},${y2 - r}V${y}Z`
    case 'right':
      return `M${x},${y}H${x2 - r}Q${x2},${y} ${x2},${y + r}V${y2 - r}Q${x2},${y2} ${x2 - r},${y2}H${x}Z`
    case 'left':
      return `M${x2},${y}H${x + r}Q${x},${y} ${x},${y + r}V${y2 - r}Q${x},${y2} ${x + r},${y2}H${x2}Z`
  }
}

function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    setWidth(Math.floor(element.getBoundingClientRect().width))
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

type Tip = { x: number; y: number; title: string; lines: string[] }

function pointerIn(event: MouseEvent<SVGElement>) {
  const svg = event.currentTarget.ownerSVGElement ?? (event.currentTarget as SVGSVGElement)
  const box = svg.getBoundingClientRect()
  return { x: event.clientX - box.left, y: event.clientY - box.top }
}

// ---------------------------------------------------------------- component

export function DataChart({ spec }: { spec: ChartSpec }) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const [showTable, setShowTable] = useState(false)
  const [tip, setTip] = useState<Tip | null>(null)

  let plot: ReactNode = null
  if (width > 0) {
    const props = { width, onTip: setTip }
    if (spec.type === 'bar') plot = <BarPlot spec={spec} {...props} />
    else if (spec.type === 'histogram') plot = <HistogramPlot spec={spec} {...props} />
    else if (spec.type === 'line') plot = <LinePlot spec={spec} {...props} />
    else if (spec.type === 'scatter') plot = <ScatterPlot spec={spec} {...props} />
    else plot = <HeatmapPlot spec={spec} {...props} />
  }

  return (
    <figure className="data-chart">
      <figcaption className="data-chart-header">
        <div>
          <strong>{spec.title}</strong>
          {spec.subtitle && <span>{spec.subtitle}</span>}
        </div>
        <button type="button" className="data-chart-toggle" aria-pressed={showTable} onClick={() => setShowTable(value => !value)}>
          {showTable ? 'Chart' : 'Table'}
        </button>
      </figcaption>
      <div ref={containerRef} className="data-chart-plot" onMouseLeave={() => setTip(null)}>
        {showTable ? <ChartTable spec={spec} /> : plot}
        {!showTable && tip && (
          <div
            className="data-chart-tooltip"
            role="status"
            style={{ left: Math.min(Math.max(tip.x + 12, 0), Math.max(0, width - 190)), top: Math.max(0, tip.y - 12) }}
          >
            <strong>{tip.title}</strong>
            {tip.lines.map(line => <span key={line}>{line}</span>)}
          </div>
        )}
      </div>
    </figure>
  )
}

type PlotProps<T extends ChartSpec['type']> = {
  spec: Extract<ChartSpec, { type: T }>
  width: number
  onTip: (tip: Tip | null) => void
}

function YAxis({ ticks, y, left, right }: { ticks: number[]; y: (value: number) => number; left: number; right: number }) {
  return (
    <g className="data-chart-axis">
      {ticks.map(tick => (
        <g key={tick}>
          <line x1={left} x2={right} y1={y(tick)} y2={y(tick)} className={tick === 0 ? 'data-chart-baseline' : 'data-chart-grid'} />
          <text x={left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{formatTick(tick)}</text>
        </g>
      ))}
    </g>
  )
}

function AxisTitles({ width, height, x, y, top }: { width: number; height: number; x?: string; y?: string; top: number }) {
  return (
    <g className="data-chart-axis-title">
      {y && <text x={4} y={top - 10}>{truncate(y, 40)}</text>}
      {x && <text x={width / 2} y={height - 4} textAnchor="middle">{truncate(x, 60)}</text>}
    </g>
  )
}

function BarPlot({ spec, width, onTip }: PlotProps<'bar'>) {
  const rowHeight = 30
  const barThickness = 18
  const longest = Math.max(...spec.categories.map(category => category.length))
  const left = Math.min(Math.max(56, longest * 7 + 16), Math.floor(width * 0.4))
  const right = 64
  const top = 6
  const height = top + spec.categories.length * rowHeight + 24
  const values = spec.values.map(value => value ?? 0)
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), width < 420 ? 3 : 5)
  const x = scale([ticks[0], ticks[ticks.length - 1]], [left, width - right])
  const maxLabelChars = Math.floor((left - 12) / 7)

  return (
    <svg width={width} height={height} role="img" aria-label={spec.title}>
      <g className="data-chart-axis">
        {ticks.map(tick => (
          <g key={tick}>
            <line x1={x(tick)} x2={x(tick)} y1={top} y2={height - 20} className={tick === 0 ? 'data-chart-baseline' : 'data-chart-grid'} />
            <text x={x(tick)} y={height - 6} textAnchor="middle">{formatTick(tick)}</text>
          </g>
        ))}
      </g>
      {spec.categories.map((category, index) => {
        const value = spec.values[index]
        const rowTop = top + index * rowHeight
        const barY = rowTop + (rowHeight - barThickness) / 2
        const start = x(Math.min(0, value ?? 0))
        const end = x(Math.max(0, value ?? 0))
        const negative = (value ?? 0) < 0
        const show = (event: MouseEvent<SVGElement>) => onTip({ ...pointerIn(event), title: category, lines: [`${spec.value_label || 'Value'}: ${formatValue(value, spec.unit)}`] })
        return (
          <g key={`${category}-${index}`} className="data-chart-hit" onMouseMove={show} onMouseEnter={show}>
            <rect x={0} y={rowTop} width={width} height={rowHeight} fill="transparent" />
            <text className="data-chart-label" x={left - 8} y={rowTop + rowHeight / 2} dy="0.32em" textAnchor="end">
              <title>{category}</title>
              {truncate(category, maxLabelChars)}
            </text>
            {value !== null && end - start > 0 && (
              <path className="data-chart-mark" d={barPath(start, barY, end - start, barThickness, negative ? 'left' : 'right')} />
            )}
            <text className="data-chart-value" x={negative ? start - 6 : end + 6} y={rowTop + rowHeight / 2} dy="0.32em" textAnchor={negative ? 'end' : 'start'}>
              {value === null ? 'n/a' : `${formatTick(value)}${spec.unit ?? ''}`}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function HistogramPlot({ spec, width, onTip }: PlotProps<'histogram'>) {
  const height = 240
  const margin = { top: 24, right: 12, bottom: spec.x_label ? 42 : 26, left: 48 }
  const bins = spec.bins
  const ticks = niceTicks(0, Math.max(...bins.map(bin => bin.count)), 4)
  const y = scale([0, ticks[ticks.length - 1]], [height - margin.bottom, margin.top])
  const x = scale([bins[0].start, bins[bins.length - 1].end], [margin.left, width - margin.right])
  const labelEvery = Math.max(1, Math.ceil(bins.length / Math.max(2, Math.floor((width - margin.left) / 70))))

  return (
    <svg width={width} height={height} role="img" aria-label={spec.title}>
      <YAxis ticks={ticks} y={y} left={margin.left} right={width - margin.right} />
      <AxisTitles width={width} height={height} x={spec.x_label} y={spec.y_label} top={margin.top} />
      {bins.map((bin, index) => {
        // 2px surface gap between touching columns.
        const x0 = x(bin.start) + 1
        const barWidth = Math.max(1, x(bin.end) - x(bin.start) - 2)
        const show = (event: MouseEvent<SVGElement>) => onTip({ ...pointerIn(event), title: `${formatValue(bin.start)} – ${formatValue(bin.end)}`, lines: [`${spec.y_label || 'Count'}: ${fullFormat.format(bin.count)}`] })
        return (
          <g key={index} className="data-chart-hit" onMouseMove={show} onMouseEnter={show}>
            <rect x={x0 - 1} y={margin.top} width={barWidth + 2} height={height - margin.top - margin.bottom} fill="transparent" />
            {bin.count > 0 && <path className="data-chart-mark" d={barPath(x0, y(bin.count), barWidth, y(0) - y(bin.count), 'top')} />}
          </g>
        )
      })}
      <g className="data-chart-axis">
        {bins.map((bin, index) => index % labelEvery === 0 && (
          <text key={index} x={x(bin.start)} y={height - margin.bottom + 16} textAnchor="middle">{formatTick(bin.start)}</text>
        ))}
      </g>
    </svg>
  )
}

function LinePlot({ spec, width, onTip }: PlotProps<'line'>) {
  const [hover, setHover] = useState<number | null>(null)
  const height = 240
  const margin = { top: 24, right: 56, bottom: spec.x_label ? 42 : 26, left: 52 }
  const present = spec.y.filter((value): value is number => value !== null)
  const ticks = niceTicks(Math.min(0, ...present), Math.max(...present, 0), 4)
  const y = scale([ticks[0], ticks[ticks.length - 1]], [height - margin.bottom, margin.top])
  const x = scale([0, Math.max(1, spec.x.length - 1)], [margin.left, width - margin.right])
  const points = spec.y.map((value, index) => (value === null ? null : ([x(index), y(value)] as const)))
  const segments = points.reduce<string[]>((paths, point, index) => {
    if (!point) return paths
    const command = index > 0 && points[index - 1] ? 'L' : 'M'
    paths.push(`${command}${point[0]},${point[1]}`)
    return paths
  }, [])
  const linePath = segments.join('')
  const valid = points.filter((point): point is readonly [number, number] => point !== null)
  const areaPath = valid.length > 1 ? `M${valid[0][0]},${y(Math.max(0, ticks[0]))}${valid.map(([px, py]) => `L${px},${py}`).join('')}L${valid[valid.length - 1][0]},${y(Math.max(0, ticks[0]))}Z` : ''
  const lastIndex = spec.y.map(value => value !== null).lastIndexOf(true)
  const labelEvery = Math.max(1, Math.ceil(spec.x.length / Math.max(2, Math.floor((width - margin.left - margin.right) / 80))))

  const handleMove = (event: MouseEvent<SVGElement>) => {
    const { x: px, y: py } = pointerIn(event)
    const index = Math.round(((px - margin.left) / Math.max(1, width - margin.left - margin.right)) * (spec.x.length - 1))
    const clamped = Math.min(spec.x.length - 1, Math.max(0, index))
    setHover(clamped)
    onTip({ x: px, y: py, title: spec.x[clamped], lines: [`${spec.y_label || 'Value'}: ${formatValue(spec.y[clamped])}`] })
  }

  return (
    <svg width={width} height={height} role="img" aria-label={spec.title} onMouseMove={handleMove} onMouseLeave={() => setHover(null)}>
      <YAxis ticks={ticks} y={y} left={margin.left} right={width - margin.right} />
      <AxisTitles width={width} height={height} x={spec.x_label} y={spec.y_label} top={margin.top} />
      <g className="data-chart-axis">
        {spec.x.map((label, index) => index % labelEvery === 0 && (
          <text key={index} x={x(index)} y={height - margin.bottom + 16} textAnchor="middle">{truncate(label, 12)}</text>
        ))}
      </g>
      {areaPath && <path className="data-chart-area" d={areaPath} />}
      <path className="data-chart-line" d={linePath} />
      {hover !== null && (
        <line className="data-chart-crosshair" x1={x(hover)} x2={x(hover)} y1={margin.top} y2={height - margin.bottom} />
      )}
      {hover !== null && points[hover] && <circle className="data-chart-dot" cx={points[hover]![0]} cy={points[hover]![1]} r={4} />}
      {lastIndex >= 0 && points[lastIndex] && (
        <>
          <circle className="data-chart-dot" cx={points[lastIndex]![0]} cy={points[lastIndex]![1]} r={4} />
          <text className="data-chart-value" x={points[lastIndex]![0] + 8} y={points[lastIndex]![1]} dy="0.32em">
            {formatTick(spec.y[lastIndex]!)}
          </text>
        </>
      )}
    </svg>
  )
}

function ScatterPlot({ spec, width, onTip }: PlotProps<'scatter'>) {
  const height = 260
  const margin = { top: 24, right: 16, bottom: spec.x_label ? 42 : 26, left: 52 }
  const xs = spec.points.map(point => point[0])
  const ys = spec.points.map(point => point[1])
  const xTicks = niceTicks(Math.min(...xs), Math.max(...xs), width < 420 ? 3 : 5)
  const yTicks = niceTicks(Math.min(...ys), Math.max(...ys), 4)
  const x = scale([xTicks[0], xTicks[xTicks.length - 1]], [margin.left, width - margin.right])
  const y = scale([yTicks[0], yTicks[yTicks.length - 1]], [height - margin.bottom, margin.top])
  const [hover, setHover] = useState<number | null>(null)

  const handleMove = (event: MouseEvent<SVGElement>) => {
    const { x: px, y: py } = pointerIn(event)
    let best = -1
    let bestDistance = 14 * 14
    spec.points.forEach(([vx, vy], index) => {
      const distance = (x(vx) - px) ** 2 + (y(vy) - py) ** 2
      if (distance < bestDistance) {
        bestDistance = distance
        best = index
      }
    })
    setHover(best >= 0 ? best : null)
    onTip(best >= 0
      ? { x: px, y: py, title: `Point ${best + 1}`, lines: [`${spec.x_label || 'x'}: ${formatValue(spec.points[best][0])}`, `${spec.y_label || 'y'}: ${formatValue(spec.points[best][1])}`] }
      : null)
  }

  return (
    <svg width={width} height={height} role="img" aria-label={spec.title} onMouseMove={handleMove} onMouseLeave={() => setHover(null)}>
      <YAxis ticks={yTicks} y={y} left={margin.left} right={width - margin.right} />
      <AxisTitles width={width} height={height} x={spec.x_label} y={spec.y_label} top={margin.top} />
      <g className="data-chart-axis">
        {xTicks.map(tick => (
          <text key={tick} x={x(tick)} y={height - margin.bottom + 16} textAnchor="middle">{formatTick(tick)}</text>
        ))}
      </g>
      <g className="data-chart-points">
        {spec.points.map(([vx, vy], index) => <circle key={index} cx={x(vx)} cy={y(vy)} r={4} />)}
      </g>
      {hover !== null && <circle className="data-chart-dot" cx={x(spec.points[hover][0])} cy={y(spec.points[hover][1])} r={5} />}
    </svg>
  )
}

function HeatmapPlot({ spec, width, onTip }: PlotProps<'heatmap'>) {
  const count = spec.labels.length
  const left = Math.min(150, Math.max(60, Math.floor(width * 0.28)))
  const available = width - left - 8
  const cell = Math.max(22, Math.min(56, Math.floor(available / count)))
  const bottom = 86
  const height = count * cell + bottom + 4
  const labelChars = Math.floor((left - 10) / 6.5)
  const showValues = cell >= 34

  return (
    <svg width={Math.max(width, left + cell * count + 8)} height={height} role="img" aria-label={spec.title}>
      {spec.labels.map((label, row) => (
        <text key={`row-${label}`} className="data-chart-label" x={left - 8} y={row * cell + cell / 2} dy="0.32em" textAnchor="end">
          <title>{label}</title>
          {truncate(label, labelChars)}
        </text>
      ))}
      {spec.labels.map((label, column) => (
        <text
          key={`col-${label}`}
          className="data-chart-label"
          transform={`translate(${left + column * cell + cell / 2},${count * cell + 10}) rotate(-40)`}
          textAnchor="end"
          dy="0.32em"
        >
          <title>{label}</title>
          {truncate(label, 14)}
        </text>
      ))}
      {spec.matrix.map((values, row) => values.map((value, column) => {
        const strength = value === null ? 0 : Math.min(1, Math.abs(value))
        const pole = value !== null && value < 0 ? 'var(--viz-negative)' : 'var(--viz-positive)'
        const show = (event: MouseEvent<SVGElement>) => onTip({ ...pointerIn(event), title: `${spec.labels[row]} × ${spec.labels[column]}`, lines: [`r = ${value === null ? 'n/a' : value.toFixed(2)}`] })
        return (
          <g key={`${row}-${column}`} className="data-chart-hit" onMouseMove={show} onMouseEnter={show}>
            <rect
              x={left + column * cell + 1}
              y={row * cell + 1}
              width={cell - 2}
              height={cell - 2}
              rx={3}
              style={{ fill: `color-mix(in oklab, ${pole} ${Math.round(strength * 100)}%, var(--viz-neutral))` }}
            />
            {showValues && value !== null && (
              <text
                className={strength > 0.55 ? 'data-chart-cell-text data-chart-cell-text-inverse' : 'data-chart-cell-text'}
                x={left + column * cell + cell / 2}
                y={row * cell + cell / 2}
                dy="0.32em"
                textAnchor="middle"
              >
                {value.toFixed(2)}
              </text>
            )}
          </g>
        )
      }))}
    </svg>
  )
}

function ChartTable({ spec }: { spec: ChartSpec }) {
  const { head, rows } = useMemo(() => {
    switch (spec.type) {
      case 'bar':
        return { head: ['Category', spec.value_label || 'Value'], rows: spec.categories.map((category, index) => [category, formatValue(spec.values[index], spec.unit)]) }
      case 'histogram':
        return { head: [spec.x_label || 'Range', spec.y_label || 'Count'], rows: spec.bins.map(bin => [`${formatValue(bin.start)} – ${formatValue(bin.end)}`, fullFormat.format(bin.count)]) }
      case 'line':
        return { head: [spec.x_label || 'x', spec.y_label || 'Value'], rows: spec.x.map((label, index) => [label, formatValue(spec.y[index])]) }
      case 'scatter':
        return { head: [spec.x_label || 'x', spec.y_label || 'y'], rows: spec.points.map(([vx, vy]) => [formatValue(vx), formatValue(vy)]) }
      case 'heatmap':
        return { head: ['', ...spec.labels], rows: spec.matrix.map((values, row) => [spec.labels[row], ...values.map(value => (value === null ? 'n/a' : value.toFixed(2)))]) }
    }
  }, [spec])

  return (
    <div className="data-chart-table">
      <table>
        <thead><tr>{head.map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}
