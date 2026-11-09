import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import ReferenceActionButton from './ReferenceActionButton';

describe('ReferenceActionButton', () => {
  it.each(['清除', '上传', '替换'])('keeps %s readable with a minimum target and visible keyboard focus', (label) => {
    const html = renderToStaticMarkup(createElement(ReferenceActionButton, {
      children: label, onClick: () => {}, variant: label === '清除' ? 'context' : 'card',
    }));
    expect(html).toContain('type="button"');
    expect(html).toContain('min-h-11 min-w-11');
    expect(html).toContain('focus-visible:outline-2');
    expect(html).toContain('focus-visible:outline-offset-2');
    expect(html).toContain(`>${label}</button>`);
    expect(html).not.toContain('disabled=""');
  });

  it('preserves native disabled state and the original handler without triggering it on render', () => {
    const onClick = vi.fn();
    const button = ReferenceActionButton({ children: '上传', onClick, disabled: true });
    expect(button.props.onClick).toBe(onClick);
    expect(button.props.disabled).toBe(true);
    expect(renderToStaticMarkup(button)).toContain('disabled=""');
    expect(onClick).not.toHaveBeenCalled();
  });
});
