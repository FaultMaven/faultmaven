'use client';

import { Check, Handshake, Key, Megaphone, Mail, Info } from 'lucide-react';
import Button from '@/components/ui/Button';
import { useState, useRef, useEffect } from 'react';
import { z } from 'zod';
import dynamic from 'next/dynamic';

// Dynamically import the Benefits component
const Benefits = dynamic(() => import('@/components/sections/Benefits'), {
  loading: () => (
    <div className="py-20 flex items-center justify-center bg-slate-50 dark:bg-slate-800/50">
      <div className="max-w-7xl w-full px-6">
        <div className="animate-pulse space-y-8">
          <div className="h-8 bg-slate-200 dark:bg-slate-700 rounded w-1/3 mx-auto"></div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-48 bg-slate-200 dark:bg-slate-700 rounded"></div>
            ))}
          </div>
        </div>
      </div>
    </div>
  ),
  ssr: false
});

const benefits = [
  {
    icon: Key,
    title: 'Priority Access',
    description:
      'Be at the front of the line when FaultMaven 1.0 early access spots become available.',
    accent: 'bg-blue-600',
  },
  {
    icon: Megaphone,
    title: 'Exclusive Updates',
    description:
      'Receive direct-from-the-source news on our development progress, key milestones, and feature unveilings.',
    accent: 'bg-green-500',
  },
  {
    icon: Handshake,
    title: 'An Opportunity to Influence',
    description:
      'As an early community member, you\'ll have the chance to provide invaluable feedback and directly contribute to shaping a tool built for engineers, by engineers.',
    accent: 'bg-yellow-400',
  },
];

export default function WaitlistPage() {
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const emailInputRef = useRef<HTMLInputElement>(null);
  const successMessageRef = useRef<HTMLParagraphElement>(null);

  const emailSchema = z.string().email({ message: 'Please enter a valid email address.' });

  // Focus management
  useEffect(() => {
    if (success && successMessageRef.current) {
      successMessageRef.current.focus();
    }
  }, [success]);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setSuccess(false);
    setError(null);
    
    // Client-side validation
    const result = emailSchema.safeParse(email);
    if (!result.success) {
      setError(result.error.errors[0].message);
      emailInputRef.current?.focus();
      return;
    }

    setIsLoading(true);
    try {
      const response = await fetch('/api/waitlist', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to join waitlist');
      }

      setSuccess(true);
      setEmail('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred while joining the waitlist');
      emailInputRef.current?.focus();
    } finally {
      setIsLoading(false);
    }
  };

  // Handle keyboard navigation
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setError(null);
      emailInputRef.current?.focus();
    }
  };

  return (
    <main 
      className="min-h-screen bg-gradient-to-b from-blue-50/60 via-white to-slate-100 dark:from-slate-900 dark:via-slate-900 dark:to-slate-800"
      onKeyDown={handleKeyDown}
    >
      {/* Hero Section */}
      <section className="py-20 flex items-center justify-center" aria-labelledby="hero-heading">
        <div className="max-w-3xl w-full px-6">
          <div className="rounded-2xl shadow-xl bg-white/90 dark:bg-slate-900/90 p-10 border border-slate-200 dark:border-slate-800 text-center">
            <h1 id="hero-heading" className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
              Get Early Access to FaultMaven & Help Shape the Future of AI Troubleshooting
            </h1>
            <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto">
              You&apos;re one step closer to transforming how you tackle complex operational challenges. FaultMaven 1.0, your personal AI Copilot, is currently in active development, and we&apos;re inviting forward-thinking engineers like you to be among the first to experience its power.
            </p>
          </div>
        </div>
      </section>

      {/* Benefits Section */}
      <Benefits />

      {/* Signup Form Section */}
      <section 
        className="py-20 flex items-center justify-center bg-white dark:bg-slate-900" 
        aria-labelledby="signup-heading"
      >
        <div className="max-w-xl w-full px-6">
          <div className="rounded-2xl shadow-xl bg-slate-50 dark:bg-slate-800/50 p-10 border border-slate-200 dark:border-slate-700 text-center">
            <Mail className="w-10 h-10 text-blue-600 dark:text-blue-400 mb-4 mx-auto" aria-hidden="true" />
            <h2 id="signup-heading" className="text-2xl md:text-3xl font-bold text-slate-900 dark:text-slate-50 mb-4">
              Ready to get started?
            </h2>
            <p className="mb-6 text-slate-600 dark:text-slate-400">
              We invite you to join our waitlist and be part of FaultMaven&apos;s early journey. Please provide your email address below to receive exclusive updates on our progress and to be considered for early access opportunities. We&apos;re excited to connect with you!
            </p>
            <form 
              onSubmit={handleSubmit} 
              className="mt-4 max-w-md mx-auto"
              aria-describedby={error ? 'email-error' : success ? 'success-message' : undefined}
              noValidate
            >
              <div className="flex flex-col sm:flex-row gap-4">
                <div className="flex-grow">
                  <label htmlFor="email" className="sr-only">Email address</label>
                  <input
                    ref={emailInputRef}
                    id="email"
                    type="email"
                    placeholder="Enter your email address"
                    required
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    disabled={isLoading}
                    className={`w-full px-4 py-2 rounded-md text-slate-900 bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 focus:ring-2 focus:ring-blue-500 ${error ? 'border-red-500 focus:ring-red-500' : ''} ${isLoading ? 'opacity-50 cursor-not-allowed' : ''}`}
                    aria-invalid={!!error}
                    aria-describedby={error ? 'email-error' : undefined}
                    aria-required="true"
                    autoComplete="email"
                  />
                </div>
                <Button
                  type="submit"
                  variant="primary"
                  className={`min-w-[150px] px-6 py-2 gap-2 whitespace-nowrap flex-shrink-0 ${isLoading ? 'opacity-50 cursor-not-allowed' : ''}`}
                  disabled={isLoading}
                  aria-busy={isLoading}
                >
                  {isLoading ? (
                    <>
                      <span className="animate-spin" aria-hidden="true">⟳</span>
                      <span>Joining...</span>
                    </>
                  ) : (
                    <>
                      <Check className="w-5 h-5 mr-1" aria-hidden="true" />
                      <span>Join Waitlist</span>
                    </>
                  )}
                </Button>
              </div>
              {error && (
                <p 
                  id="email-error" 
                  className="mt-2 text-sm text-red-600 dark:text-red-400 text-left" 
                  role="alert"
                  aria-live="polite"
                >
                  {error}
                </p>
              )}
              {success && (
                <p 
                  ref={successMessageRef}
                  id="success-message" 
                  className="mt-2 text-sm text-green-600 dark:text-green-400 text-left" 
                  role="status"
                  aria-live="polite"
                  tabIndex={-1}
                >
                  Thank you! You have been added to the waitlist.
                </p>
              )}
            </form>
          </div>
        </div>
      </section>

      {/* What to Expect Section */}
      <section className="py-12 px-6" aria-labelledby="expectations-heading">
        <div className="max-w-2xl mx-auto">
          <div className="rounded-xl bg-blue-50 dark:bg-blue-900/30 shadow-md border border-blue-100 dark:border-blue-900 p-8 flex flex-col items-center text-center">
            <Info className="w-8 h-8 text-blue-600 dark:text-blue-400 mb-2" aria-hidden="true" />
            <h3 id="expectations-heading" className="text-xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              What to Expect After Signing Up:
            </h3>
            <p className="text-base text-slate-700 dark:text-slate-300 mb-2">
              We&apos;ll send you a confirmation email shortly. After that, we&apos;ll keep you informed about FaultMaven&apos;s journey and notify you personally when early access programs matching your interest open up. We respect your privacy, and you can unsubscribe at any time.
            </p>
            <p className="text-base text-slate-700 dark:text-slate-300">
              Thank you for your interest in FaultMaven!
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
