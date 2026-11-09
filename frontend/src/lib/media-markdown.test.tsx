import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { renderMediaMarkdown } from './media-markdown';

const render = (text: string) => renderToStaticMarkup(<>{renderMediaMarkdown(text)}</>);

describe('renderMediaMarkdown', () => {
  it('renders bold and inline code', () => {
    const html = render('做一张 **红色机器人** 海报，用 `16:9`');
    expect(html).toContain('<strong');
    expect(html).toContain('红色机器人');
    expect(html).toMatch(/<code(?:\s[^>]*)?>16:9<\/code>/);
    expect(html).not.toContain('**');
  });

  it('renders headings, lists and dividers as blocks', () => {
    const html = render('# 方案\n- 构图居中\n- 白色背景\n\n1. 先出草图\n2. 再精修\n\n---');
    expect(html).toContain('<h3');
    expect(html).toContain('<ul');
    expect(html).toContain('<ol');
    expect(html).toContain('<li>构图居中</li>');
    expect(html).toContain('<hr');
  });

  it('keeps plain text as text and never emits raw html', () => {
    const html = render('你好 <img src=x onerror=alert(1)> **朋友**');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('joins blank-line separated paragraphs', () => {
    const html = render('第一段\n\n第二段');
    expect(html.match(/<p/g)?.length).toBe(2);
  });
});
