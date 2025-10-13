import { NextResponse } from 'next/server';
import { rateLimit } from '@/middleware/rateLimit';

// TODO: Implement NextAuth authentication handler when enabling user auth
// TODO: Use a distributed/global rate limiter (e.g., Redis) for production

function getIP(request: Request) {
  // Try to get IP from headers (Vercel, proxies)
  return (
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    ''
  );
}

export async function GET(request: Request) {
  const ip = getIP(request);
  if (rateLimit(ip)) {
    return NextResponse.json({ error: 'Too many requests. Please try again later.' }, { status: 429 });
  }
  return NextResponse.json({ error: 'Not implemented. Auth will be enabled in production.' }, { status: 501 });
}

export async function POST(request: Request) {
  const ip = getIP(request);
  if (rateLimit(ip)) {
    return NextResponse.json({ error: 'Too many requests. Please try again later.' }, { status: 429 });
  }
  return NextResponse.json({ error: 'Not implemented. Auth will be enabled in production.' }, { status: 501 });
} 