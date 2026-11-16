import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { ChartSettings } from '../ChartWidget';

describe('ChartSettings symbol input IME guard', () => {
  it('does not save while composing, saves on a plain Enter', () => {
    const onChange = vi.fn();
    const onClose = vi.fn();
    const { container } = render(
      <ChartSettings
        config={{ symbol: 'AAPL', interval: '1day', chartType: 'candle' }}
        onChange={onChange}
        onClose={onClose}
      />,
    );
    const input = container.querySelector('input[type="text"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
