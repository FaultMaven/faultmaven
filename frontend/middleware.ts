import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

export function middleware(request: NextRequest) {
  const response = NextResponse.next()
  
  // Security headers
  response.headers.set('X-Frame-Options', 'DENY')
  response.headers.set('X-Content-Type-Options', 'nosniff')
  response.headers.set('Referrer-Policy', 'strict-origin-when-cross-origin')
  response.headers.set(
    'Permissions-Policy', 
    'camera=(), microphone=(), geolocation=(), browsing-topics=()'
  )
  
  // Strict Content Security Policy for pre-launch
  // TODO: Loosen CSP for payment/auth/analytics services when needed, and allow Resend API for email forwarding
  response.headers.set(
    'Content-Security-Policy',
    [
      "default-src 'self';",
      "script-src 'self';",
      "style-src 'self' 'unsafe-inline';",
      "img-src 'self' data:;",
      "font-src 'self';",
      "connect-src 'self';",
      "frame-ancestors 'none';",
      "form-action 'self';",
      "base-uri 'self';"
    ].join(' ')
  );
  
  return response
}
