import { useEffect, useRef, useState } from 'react';
import Input from './Input';
import { cn } from '../../lib/utils';

const parse = (raw) => (raw.trim() === '' ? NaN : Number(raw));

const NumberInput = ({
  id,
  value,
  onValueChange,
  onValidityChange,
  min,
  max,
  step,
  integer = false,
  hint = '',
  className,
  ...props
}) => {
  const [text, setText] = useState(String(value));
  const [prevValue, setPrevValue] = useState(value);

  if (prevValue !== value) {
    setPrevValue(value);
    if (parse(text) !== value) setText(String(value));
  }

  const inRange = (n) =>
    Number.isFinite(n) &&
    (!integer || Number.isInteger(n)) &&
    (min === undefined || n >= min) &&
    (max === undefined || n <= max);

  const bounds =
    min !== undefined && max !== undefined
      ? ` ${min}–${max} 之间`
      : min !== undefined
        ? `不小于 ${min} `
        : `不大于 ${max} `;
  const error = inRange(parse(text))
    ? ''
    : `请输入${bounds}的${integer ? '整数' : '数值'}`;
  const hintId = `${id}-hint`;

  const onValidityChangeRef = useRef(onValidityChange);
  useEffect(() => {
    onValidityChangeRef.current = onValidityChange;
  });
  useEffect(() => {
    onValidityChangeRef.current?.(!error);
    return () => onValidityChangeRef.current?.(true);
  }, [error]);

  const handleChange = (event) => {
    const raw = event.target.value;
    setText(raw);
    const n = parse(raw);
    if (inRange(n)) onValueChange(n);
  };

  const handleBlur = () => {
    if (!error) return;
    let n = parse(text);
    if (!Number.isFinite(n)) {
      setText(String(value));
      return;
    }
    if (integer) n = Math.round(n);
    if (min !== undefined) n = Math.max(min, n);
    if (max !== undefined) n = Math.min(max, n);
    setText(String(n));
    onValueChange(n);
  };

  return (
    <>
      <Input
        id={id}
        type="number"
        inputMode={integer ? 'numeric' : 'decimal'}
        min={min}
        max={max}
        step={step}
        value={text}
        onChange={handleChange}
        onBlur={handleBlur}
        aria-invalid={!!error}
        aria-describedby={error || hint ? hintId : undefined}
        className={cn(
          '[appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none',
          error && 'border-error focus:border-error focus:ring-error/25',
          className
        )}
        {...props}
      />
      {(error || hint) && (
        <p id={hintId} className={`text-xs ${error ? 'text-error' : 'text-on-surface-variant'}`}>
          {error || hint}
        </p>
      )}
    </>
  );
};

export default NumberInput;
