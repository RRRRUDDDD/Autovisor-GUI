import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import NumberInput from './ui/NumberInput';

afterEach(() => {
  cleanup();
});

describe('NumberInput', () => {
  it('propagates valid in-range values and suppresses invalid intermediate text', () => {
    const onValueChange = vi.fn();
    const onValidityChange = vi.fn();

    render(
      <NumberInput
        id="score"
        aria-label="分数"
        min={0}
        max={100}
        integer
        value={80}
        onValueChange={onValueChange}
        onValidityChange={onValidityChange}
      />
    );

    const input = screen.getByLabelText('分数');
    expect(input.value).toBe('80');
    expect(onValidityChange).toHaveBeenLastCalledWith(true);

    // Clearing input marks invalid without emitting 0
    fireEvent.change(input, { target: { value: '' } });
    expect(onValueChange).not.toHaveBeenCalled();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    // Non-integer value rejected when integer=true
    fireEvent.change(input, { target: { value: '12.5' } });
    expect(onValueChange).not.toHaveBeenCalled();
    expect(input.getAttribute('aria-invalid')).toBe('true');

    // Valid integer accepted
    fireEvent.change(input, { target: { value: '95' } });
    expect(onValueChange).toHaveBeenCalledWith(95);
    expect(input.getAttribute('aria-invalid')).toBe('false');
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it('clamps out-of-range numbers on blur and restores previous value when blurred empty', () => {
    const onValueChange = vi.fn();
    render(
      <NumberInput
        id="maxRedo"
        aria-label="重做次数"
        min={1}
        max={10}
        integer
        value={3}
        onValueChange={onValueChange}
      />
    );

    const input = screen.getByLabelText('重做次数');

    // Blur while empty restores last valid value without emitting NaN/0
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.blur(input);
    expect(input.value).toBe('3');
    expect(onValueChange).not.toHaveBeenCalled();

    // Blur above max clamps and emits max
    fireEvent.change(input, { target: { value: '99' } });
    fireEvent.blur(input);
    expect(input.value).toBe('10');
    expect(onValueChange).toHaveBeenCalledWith(10);

    // Blur below min clamps and emits min
    fireEvent.change(input, { target: { value: '0' } });
    fireEvent.blur(input);
    expect(input.value).toBe('1');
    expect(onValueChange).toHaveBeenLastCalledWith(1);
  });
});
