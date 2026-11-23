import React from 'react';
import { Check } from 'lucide-react';
import { cn } from '../../lib/utils';

const Switch = ({ id, checked, onChange, disabled = false, label, description }) => {
  const isChecked = Boolean(checked);
  return (
    <label
      htmlFor={id}
      className={cn(
        'group flex items-center justify-between gap-4 rounded-2xl px-3 py-2 -mx-3 transition-colors duration-150 ease-m3 hover:bg-on-surface/[0.05]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
      )}
    >
      <span className="min-w-0 flex-1">
        <span className="block text-[14px] font-medium leading-snug text-on-surface">{label}</span>
        {description && (
          <span className="mt-0.5 block text-xs leading-relaxed text-on-surface-variant">
            {description}
          </span>
        )}
      </span>
      <span className="relative inline-flex shrink-0 items-center">
        <input
          id={id}
          type="checkbox"
          checked={isChecked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="peer sr-only"
        />
        {/* M3 Switch Track (52x30) */}
        <span
          aria-hidden="true"
          className={cn(
            'flex h-[30px] w-[52px] items-center rounded-full border-2 p-[3px] transition-all duration-200 ease-m3',
            isChecked
              ? 'border-primary bg-primary'
              : 'border-outline bg-surface-container-highest',
            'peer-focus-visible:ring-2 peer-focus-visible:ring-primary peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface'
          )}
        >
          {/* M3 Switch Thumb (grows from 16px to 22px with Check icon when checked) */}
          <span
            className={cn(
              'flex items-center justify-center rounded-full transition-all duration-200 ease-m3',
              isChecked
                ? 'h-[22px] w-[22px] translate-x-[20px] bg-on-primary text-primary shadow-sm'
                : 'h-4 w-4 translate-x-[2px] bg-outline text-transparent group-hover:bg-on-surface-variant'
            )}
          >
            {isChecked && <Check className="h-3.5 w-3.5" strokeWidth={3} />}
          </span>
        </span>
      </span>
    </label>
  );
};

export default Switch;
