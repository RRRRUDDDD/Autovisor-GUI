import React from 'react';
import { cn } from '../../lib/utils';

const Select = React.forwardRef(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      'h-11 w-full cursor-pointer rounded-xl border border-outline/70 bg-surface-container-lowest/90 px-3.5 text-sm text-on-surface',
      'transition-all duration-150 ease-m3 hover:border-on-surface/60',
      'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/25',
      'disabled:cursor-not-allowed disabled:bg-surface-container disabled:opacity-55',
      className
    )}
    {...props}
  />
));
Select.displayName = 'Select';

export default Select;
