import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import rehypeSanitize from 'rehype-sanitize'
import { DataChart, parseChartSpec } from '@/components/chat/DataChart'

type HastNode = { type: string; tagName?: string; value?: string; properties?: { className?: unknown }; children?: HastNode[] }

function textContent(node: HastNode): string {
  return node.type === 'text' ? node.value ?? '' : (node.children ?? []).map(textContent).join('')
}

/** Source of a fenced ```chart block, or null for any other <pre>. */
function chartSource(pre: HastNode | undefined): string | null {
  const code = pre?.children?.find(child => child.type === 'element' && child.tagName === 'code')
  const className = code?.properties?.className
  return code && Array.isArray(className) && className.includes('language-chart') ? textContent(code) : null
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize, [rehypeHighlight, { plainText: ['chart'] }]]}
        components={{
          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
          pre: ({ node, children, ...props }) => {
            const source = chartSource(node as HastNode | undefined)
            const spec = source === null ? null : parseChartSpec(source)
            if (spec) return <DataChart spec={spec} />
            if (source !== null) return <p className="data-chart-invalid">This chart could not be displayed.</p>
            return <pre {...props}>{children}</pre>
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
