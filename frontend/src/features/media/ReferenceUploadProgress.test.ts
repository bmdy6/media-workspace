import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ReferenceUploadProgress from './ReferenceUploadProgress';

describe('ReferenceUploadProgress', () => {
  it('shows explicit indeterminate upload feedback without inventing a percentage', () => {
    const html = renderToStaticMarkup(createElement(ReferenceUploadProgress, { previewUrl: 'blob:test-preview', fileName: 'reference.png' }));

    expect(html).toContain('正在上传参考图');
    expect(html).toContain('参考图上传处理中');
    expect(html).toContain('reference.png');
    expect(html).not.toContain('aria-valuenow');
    expect(html).not.toMatch(/\d+%/);
  });
});
