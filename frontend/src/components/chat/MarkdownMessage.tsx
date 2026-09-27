import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import rehypeSanitize from 'rehype-sanitize'
import { DataChart, parseChartSpec } from '@/components/chat/DataChart'
import { DownloadCard, parseDownloadSpec } from '@/components/chat/DownloadCard'

type HastNode = { type: string; tagName?: string; value?: string; properties?: { className?: unknown }; children?: HastNode[] }

function textContent(node: HastNode): string {
  return node.type === 'text' ? node.value ?? '' : (node.children ?? []).map(textContent).join('')
}

/** Language and source of a fenced ```chart / ```download block, or null for any other <pre>. */
function fencedBlock(pre: HastNode | undefined): { language: 'chart' | 'download'; source: string } | null {
  const code = pre?.children?.find(child => child.type === 'element' && child.tagName === 'code')
  const className = code?.properties?.className
  if (!code || !Array.isArray(className)) return null
  if (className.includes('language-chart')) return { language: 'chart', source: textContent(code) }
  if (className.includes('language-download')) return { language: 'download', source: textContent(code) }
  return null
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize, [rehypeHighlight, { plainText: ['chart', 'download'] }]]}
        components={{
          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
          pre: ({ node, children, ...props }) => {
            const block = fencedBlock(node as HastNode | undefined)
            if (block?.language === 'download') {
              const spec = parseDownloadSpec(block.source)
              return spec ? <DownloadCard spec={spec} /> : <p className="data-chart-invalid">This file could not be offered for download.</p>
            }
            const spec = block ? parseChartSpec(block.source) : null
            if (spec) return <DataChart spec={spec} />
            if (block) return <p className="data-chart-invalid">This chart could not be displayed.</p>
            return <pre {...props}>{children}</pre>
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
