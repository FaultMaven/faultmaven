'use client';

import { Check, Handshake, Key, Megaphone } from 'lucide-react';

const benefits = [
  {
    icon: Key,
    title: 'Priority Access',
    description:
      'Be at the front of the line when FaultMaven 1.0 early access spots become available.',
  },
  {
    icon: Megaphone,
    title: 'Exclusive Updates',
    description:
      'Receive direct-from-the-source news on our development progress, key milestones, and feature unveilings.',
  },
  {
    icon: Handshake,
    title: 'An Opportunity to Influence',
    description:
      'As an early community member, you&apos;ll have the chance to provide invaluable feedback and directly contribute to shaping a tool built for engineers, by engineers.',
  },
];

export default function WaitlistPage() {
  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Handle form submission logic
    console.log('Form submitted');
  };

  return (
    <main>
      <section className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <h1 className="text-3xl md:text-4xl font-bold text-slate-900 dark:text-slate-50">
            Get Early Access to FaultMaven & Help Shape the Future of AI
            Troubleshooting
          </h1>
          <p className="mt-6 text-lg max-w-3xl mx-auto text-slate-600 dark:text-slate-400">
            You&apos;re one step closer to transforming how you tackle complex
            operational challenges. FaultMaven 1.0, your personal AI Copilot,
            is currently in active development, and we&apos;re inviting
            forward-thinking engineers like you to be among the first to
            experience its power.
          </p>
        </div>
      </section>

      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-center font-semibold text-slate-800 dark:text-slate-200 mb-8">
            By joining our waitlist, you&apos;ll get:
          </h2>
          <div className="grid md:grid-cols-3 gap-8">
            {benefits.map((benefit, index) => (
              <div
                key={index}
                className="bg-white dark:bg-slate-900 p-6 rounded-lg shadow-md"
              >
                <div className="flex items-start gap-4">
                  <div className="flex-shrink-0 w-8 h-8 flex items-center justify-center">
                    <benefit.icon className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                  </div>
                  <div>
                    <h3 className="font-semibold text-slate-800 dark:text-slate-200">
                      {benefit.title}
                    </h3>
                    <p className="text-sm text-slate-600 dark:text-slate-400 mt-1">
                      {benefit.description}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-2xl mx-auto px-6 text-center">
          <h2 className="text-2xl font-semibold text-slate-800 dark:text-slate-200">
            Ready to get started?
          </h2>
          <p className="mt-4 text-slate-600 dark:text-slate-400">
            We invite you to join our waitlist and be part of FaultMaven&apos;s early
            journey. Please provide your email address below to receive
            exclusive updates on our progress and to be considered for early
            access opportunities. We&apos;re excited to connect with you!
          </p>
          <form onSubmit={handleSubmit} className="mt-8 max-w-md mx-auto">
            <div className="flex flex-col sm:flex-row gap-4">
              <input
                type="email"
                placeholder="Enter your email address"
                required
                className="flex-grow px-4 py-3 rounded-md text-slate-900 bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 focus:ring-2 focus:ring-blue-500"
              />
              <button
                type="submit"
                className="inline-flex items-center justify-center px-6 py-3 font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 transition duration-200"
                style={{ backgroundColor: '#2563EB' }}
              >
                Join the Waitlist
              </button>
            </div>
          </form>
          <div className="mt-8 text-sm text-slate-500 dark:text-slate-400">
            <h3 className="font-semibold">What to Expect After Signing Up:</h3>
            <p className="mt-2">
              We&apos;ll send you a confirmation email shortly. After that, we&apos;ll
              keep you informed about FaultMaven&apos;s journey and notify you
              personally when early access programs matching your interest open
              up. We respect your privacy, and you can unsubscribe at any time.
            </p>
            <p className="mt-4">Thank you for your interest in FaultMaven!</p>
          </div>
        </div>
      </section>
    </main>
  );
}
