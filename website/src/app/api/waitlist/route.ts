import { NextResponse } from 'next/server';
import { rateLimit } from '@/middleware/rateLimit';
import { z } from 'zod';
import { Resend } from 'resend';

// DEBUG: Log the API key to confirm it's loaded
console.log('RESEND_API_KEY:', process.env.RESEND_API_KEY);

// Only initialize Resend if we have an API key
const resend = process.env.RESEND_API_KEY ? new Resend(process.env.RESEND_API_KEY) : null;

const emailSchema = z.string().email({ message: 'Please enter a valid email address.' });

function getIP(request: Request) {
  return (
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    ''
  );
}

export async function POST(request: Request) {
  const ip = getIP(request);
  if (rateLimit(ip)) {
    return NextResponse.json(
      { error: 'Too many requests. Please try again later.' },
      { status: 429 }
    );
  }

  try {
    const body = await request.json();
    const { email } = body;

    // Validate email
    const result = emailSchema.safeParse(email);
    if (!result.success) {
      return NextResponse.json(
        { error: result.error.errors[0].message },
        { status: 400 }
      );
    }

    // Send confirmation email if Resend is configured
    if (resend) {
      console.log('Attempting to send confirmation email to:', email);
      try {
        await resend.emails.send({
          from: 'FaultMaven <support@faultmaven.ai>',
          to: email,
          subject: 'Welcome to the FaultMaven Waitlist!',
          html: `
            <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
              <h1 style="color: #2563EB;">Welcome to FaultMaven!</h1>
              <p>Thank you for joining our waitlist. We're excited to have you on board!</p>
              <p>We'll keep you updated on our progress and notify you when early access becomes available.</p>
              <p>Best regards,<br>The FaultMaven Team</p>
            </div>
          `,
        });
        console.log('Confirmation email sent successfully to:', email);
      } catch (emailError) {
        console.error('Error sending confirmation email:', emailError);
      }
    }

    // TODO: Store email in database
    // TODO: Add to mailing list (e.g., Mailchimp, SendGrid)

    return NextResponse.json(
      { message: 'Successfully joined the waitlist!' },
      { status: 200 }
    );
  } catch (error) {
    console.error('Waitlist submission error:', error);
    return NextResponse.json(
      { error: 'An error occurred while processing your request.' },
      { status: 500 }
    );
  }
} 