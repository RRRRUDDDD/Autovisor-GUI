import React from 'react';
import { cn } from '../../lib/utils';

const Button = React.forwardRef(
  ({ className, variant = 'default', size = 'default', children, ...props }, ref) => {
    const variantStyles = {
      default:
        'bg-primary text-on-primary shadow-elevation-1 hover:shadow-elevation-2 hover:brightness-[1.06] active:scale-[0.99]',
      tonal:
        'bg-secondary-container text-on-secondary-container hover:shadow-elevation-1 hover:brightness-[0.97] dark:hover:brightness-[1.08] active:scale-[0.99]',
      'primary-tonal':
        'bg-primary-container text-on-primary-container hover:shadow-elevation-1 hover:brightness-[0.97] dark:hover:brightness-[1.08] active:scale-[0.99]',
      destructive:
        'bg-error text-on-error shadow-elevation-1 hover:shadow-elevation-2 hover:brightness-[1.06] active:scale-[0.99]',
      'destructive-tonal':
        'bg-error-container text-on-error-container hover:brightness-[0.96] dark:hover:brightness-[1.08] active:scale-[0.99]',
      outline:
        'border border-outline/80 bg-transparent text-primary hover:bg-primary/10 active:bg-primary/15',
      ghost:
        'bg-transparent text-on-surface-variant hover:bg-on-surface/8 hover:text-on-surface active:bg-on-surface/12',
    };

    const sizeStyles = {
      default: 'h-10 px-5 text-sm',
      sm: 'h-8 px-3.5 text-[13px]',
      lg: 'h-12 px-6 text-[15px]',
      icon: 'h-10 w-10 p-0',
    };

    return (
      <button
        className={cn(
          'inline-flex select-none items-center justify-center gap-2 rounded-full font-medium tracking-[0.01em]',
          'transition-all duration-200 ease-m3',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-surface',
          variantStyles[variant] || variantStyles.default,
          sizeStyles[size] || sizeStyles.default,
          'disabled:pointer-events-none disabled:border-outline-variant/40 disabled:bg-on-surface/[0.12] disabled:text-on-surface/[0.45] disabled:shadow-none',
          className
        )}
        ref={ref}
        {...props}
      >
        {children}
      </button>
    );
  }
);

Button.displayName = 'Button';

export default Button;
