import NextLink from 'next/link'
import clsx from 'clsx'

interface LinkProps extends React.AnchorHTMLAttributes<HTMLAnchorElement> {
  href: string
  children: React.ReactNode
  variant?: 'tertiary'
}

export default function Link({ href, children, variant = 'tertiary', className, ...props }: LinkProps) {
  const base = 'transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2'
  const tertiary = 'hover:underline'
  return (
    <NextLink 
      href={href}
      className={clsx(base, variant === 'tertiary' && tertiary, className)}
      {...props}
    >
      {children}
    </NextLink>
  )
}
