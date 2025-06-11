'use client';

import { Check, Handshake, Key, Megaphone, Mail, Info } from 'lucide-react';
import Button from '@/components/ui/Button';

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
  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Handle form submission logic
    console.log('Form submitted');
  };

  return (
    <main className="min-h-screen bg-gradient-to-b from-blue-50/60 via-white to-slate-100 dark:from-slate-900 dark:via-slate-900 dark:to-slate-800">
      {/* Hero Section */}
      <section className="py-20 flex items-center justify-center">
        <div className="max-w-3xl w-full px-6">
          <div className="rounded-2xl shadow-xl bg-white/90 dark:bg-slate-900/90 p-10 border border-slate-200 dark:border-slate-800 text-center">
            <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
              Get Early Access to FaultMaven & Help Shape the Future of AI Troubleshooting
            </h1>
            <p className="text-xl text-slate-700 dark:text-slate-300 max-w-2xl mx-auto">
              You&apos;re one step closer to transforming how you tackle complex operational challenges. FaultMaven 1.0, your personal AI Copilot, is currently in active development, and we&apos;re inviting forward-thinking engineers like you to be among the first to experience its power.
            </p>
          </div>
        </div>
      </section>

      {/* Benefits Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-center font-semibold text-slate-800 dark:text-slate-200 mb-10 text-2xl md:text-3xl">
            By joining our waitlist, you&apos;ll get:
          </h2>
          <div className="grid md:grid-cols-3 gap-8">
            {benefits.map((benefit, index) => (
              <div
                key={index}
                className="relative bg-white dark:bg-slate-900 p-8 rounded-2xl shadow-md border border-slate-200 dark:border-slate-700 transition hover:shadow-lg group"
              >
                <div className={`absolute -top-4 left-6 w-10 h-2 rounded-full ${benefit.accent} opacity-80 group-hover:scale-105 transition`} />
                <div className="flex flex-col items-center">
                  <div className="flex-shrink-0 w-12 h-12 flex items-center justify-center rounded-full bg-blue-50 dark:bg-blue-900/30 mb-4">
                    <benefit.icon className="w-7 h-7 text-blue-600 dark:text-blue-400" />
                  </div>
                  <h3 className="font-bold text-lg text-slate-800 dark:text-slate-200 mb-2 text-center">
                    {benefit.title}
                  </h3>
                  <p className="text-base text-slate-600 dark:text-slate-400 text-center">
                    {benefit.description}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Signup Form Section */}
      <section className="py-20 flex items-center justify-center bg-white dark:bg-slate-900">
        <div className="max-w-xl w-full px-6">
          <div className="rounded-2xl shadow-xl bg-slate-50 dark:bg-slate-800/50 p-10 border border-slate-200 dark:border-slate-700 text-center">
            <Mail className="w-10 h-10 text-blue-600 dark:text-blue-400 mb-4 mx-auto" />
            <h2 className="text-2xl md:text-3xl font-bold text-slate-900 dark:text-slate-50 mb-4">
              Ready to get started?
            </h2>
            <p className="mb-6 text-slate-600 dark:text-slate-400">
              We invite you to join our waitlist and be part of FaultMaven&apos;s early journey. Please provide your email address below to receive exclusive updates on our progress and to be considered for early access opportunities. We&apos;re excited to connect with you!
            </p>
            <form onSubmit={handleSubmit} className="mt-4 max-w-md mx-auto">
              <div className="flex flex-col sm:flex-row gap-4">
                <input
                  type="email"
                  placeholder="Enter your email address"
                  required
                  className="flex-grow px-4 py-2 rounded-md text-slate-900 bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 focus:ring-2 focus:ring-blue-500"
                />
                <Button type="submit" variant="primary" className="w-40 gap-2 py-2">
                  <Check className="w-5 h-5 mr-1" />
                  Join the Waitlist
                </Button>
              </div>
            </form>
          </div>
        </div>
      </section>

      {/* What to Expect Section */}
      <section className="py-12 px-6">
        <div className="max-w-2xl mx-auto">
          <div className="rounded-xl bg-blue-50 dark:bg-blue-900/30 shadow-md border border-blue-100 dark:border-blue-900 p-8 flex flex-col items-center text-center">
            <Info className="w-8 h-8 text-blue-600 dark:text-blue-400 mb-2" />
            <h3 className="text-xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
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
