import { NextResponse } from 'next/server';

// TODO: Implement analytics webhook handler if Vercel must receive analytics webhooks directly
export async function POST() {
  return NextResponse.json({ error: 'Not implemented. Analytics webhook will be enabled in production if needed.' }, { status: 501 });
} 