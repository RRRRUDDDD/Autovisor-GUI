import React from 'react';
import { cn } from '../../lib/utils';

const Input = React.forwardRef(({ className, type = 'text', ...props }, ref) => {
  return (
    <input
      type={type}
      className={cn(
        'flex h-11 w-full rounded-xl border border-outline/70 bg-surface-container-lowest/90 px-3.5 py-2 text-sm text-on-surface',
        'placeholder:text-outline',
        'transition-all duration-150 ease-m3',
        'hover:border-on-surface/60',
        'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/25',
        'disabled:cursor-not-allowed disabled:bg-surface-container disabled:opacity-55',
        className
      )}
      ref={ref}
      {...props}
    />
  );
});

Input.displayName = 'Input';

export default Input;
