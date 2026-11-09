import type { ReactNode } from 'react';
import { Fragment } from 'react';

// 助手文案的轻量 Markdown 子集：**加粗**、`行内代码`、# 标题、- / 1. 列表、--- 分隔线、段落。
// 只输出 React 文本节点，不使用 dangerouslySetInnerHTML，因此模型输出无法注入 HTML。

const INLINE_PATTERN = /(\*\*[^*]+\*\*|`[^`]+`)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE_PATTERN).filter((part) => part !== '').map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key} className="font-semibold text-neutral-900">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key} className="rounded bg-neutral-100 px-1 py-0.5 font-mono text-[0.85em]">{part.slice(1, -1)}</code>;
    }
    return <Fragment key={key}>{part}</Fragment>;
  });
}

export function renderMediaMarkdown(text: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = text.split(/\r?\n/);
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const content = paragraph.join('\n');
    blocks.push(<p key={`p-${blocks.length}`} className="whitespace-pre-wrap">{renderInline(content, `p-${blocks.length}`)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((item, index) => <li key={index}>{renderInline(item, `li-${blocks.length}-${index}`)}</li>);
    blocks.push(list.ordered
      ? <ol key={`ol-${blocks.length}`} className="ml-5 list-decimal space-y-1">{items}</ol>
      : <ul key={`ul-${blocks.length}`} className="ml-5 list-disc space-y-1">{items}</ul>);
    list = null;
  };
  const flushAll = () => { flushParagraph(); flushList(); };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const unordered = line.match(/^[-*]\s+(.*)$/);
    const ordered = line.match(/^\d+[.、]\s+(.*)$/);
    if (!line.trim()) {
      flushAll();
    } else if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      flushAll();
      blocks.push(<hr key={`hr-${blocks.length}`} className="my-3 border-neutral-200" />);
    } else if (heading) {
      flushAll();
      const level = heading[1].length;
      blocks.push(<h3 key={`h-${blocks.length}`} className={`${level === 1 ? 'text-base' : 'text-sm'} font-semibold text-neutral-900`}>{renderInline(heading[2], `h-${blocks.length}`)}</h3>);
    } else if (unordered || ordered) {
      flushParagraph();
      const orderedItem = Boolean(ordered);
      const content = (unordered?.[1] ?? ordered?.[1] ?? '').trim();
      if (list && list.ordered !== orderedItem) flushList();
      if (!list) list = { ordered: orderedItem, items: [] };
      list.items.push(content);
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushAll();
  return blocks;
}
