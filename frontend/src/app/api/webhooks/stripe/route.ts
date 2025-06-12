import { NextResponse } from 'next/server';

// TODO: Implement Stripe webhook handler if Vercel must receive Stripe webhooks directly
export async function POST() {
  return NextResponse.json({ error: 'Not implemented. Stripe webhook will be enabled in production if needed.' }, { status: 501 });
} 