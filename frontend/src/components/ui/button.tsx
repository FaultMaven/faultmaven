import React from 'react';
import clsx from 'clsx';

export type ButtonVariant = 'primary' | 'secondary' | 'tertiary';

interface BaseButtonProps {
  variant?: ButtonVariant;
  asChild?: boolean;
  href?: string;
  children: React.ReactNode;
  className?: string;
}

// For <button>
type NativeButtonProps = BaseButtonProps & React.ButtonHTMLAttributes<HTMLButtonElement>;
// For <a>
type AnchorButtonProps = BaseButtonProps & React.AnchorHTMLAttributes<HTMLAnchorElement>;

type ButtonProps = NativeButtonProps | AnchorButtonProps;

export const buttonBase =
  'inline-flex items-center justify-center font-medium rounded-md transition focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2';

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    'bg-[#2563EB] text-white shadow-sm hover:bg-blue-700 px-6 py-3',
  secondary:
    'border border-[#2563EB] text-[#2563EB] bg-transparent hover:bg-[#2563EB] hover:text-white px-6 py-3',
  tertiary:
    'text-[#2563EB] bg-transparent hover:underline px-2 py-1',
};

export default function Button({
  variant = 'primary',
  asChild = false,
  href,
  className,
  children,
  ...props
}: ButtonProps) {
  const classes = clsx(buttonBase, variantClasses[variant], className);
  if (asChild && href) {
    // Render as a link, only spread anchor props
    const { type, ...anchorProps } = props as React.AnchorHTMLAttributes<HTMLAnchorElement>;
    return (
      <a href={href} className={classes} {...anchorProps}>
        {children}
      </a>
    );
  }
  return (
    <button className={classes} {...props as React.ButtonHTMLAttributes<HTMLButtonElement>}>
      {children}
    </button>
  );
}
