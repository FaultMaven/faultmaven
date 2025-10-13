'use client';

import Image from 'next/image';
import Button from '../ui/Button';
import heroImage from '../../../public/images/hero-mttr.webp';

export default function Hero() {
  return (
    <section className="pt-32 pb-24 bg-slate-50 dark:bg-slate-900" aria-labelledby="hero-heading">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid md:grid-cols-2 gap-16 items-center">
          <div className="text-left">
            <h1
              id="hero-heading"
              className="font-bold mb-6"
              style={{
                fontSize: 36,
                color: '#0F172A',
                lineHeight: 1.2,
              }}
            >
              Troubleshoot Faster, Smarter, with FaultMaven
            </h1>
            <p
              className="text-lg mb-10 max-w-2xl text-slate-600 dark:text-slate-400"
            >
              Your evolving AI Copilot—built <span className="italic">with</span> engineers like you—delivering real-time insights and guided solutions to dramatically reduce{' '}
              <span style={{ fontWeight: 600 }}>Mean Time To Resolution (MTTR)</span> across your toughest operational challenges.
            </p>
            <Button asChild href="/waitlist" variant="primary" className="max-w-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2">
              Get Early Access Updates & Help Shape FaultMaven
            </Button>
          </div>
          <div className="relative">
            <Image
              src={heroImage}
              alt="FaultMaven AI Copilot in action, analyzing code to reduce MTTR"
              priority
              placeholder="blur"
              className="rounded-xl shadow-2xl"
              sizes="(max-width: 768px) 100vw, 50vw"
              quality={90}
            />
          </div>
        </div>
        <div className="text-center mt-24">
          <hr className="my-10 border-slate-200 dark:border-slate-700" />
          <div
            className="mx-auto"
            style={{
              maxWidth: '40rem',
              marginTop: '2.5rem',
            }}
          >
            <p
              className="text-sm"
              style={{
                fontSize: '0.875rem',
                fontWeight: 400,
                color: 'var(--tw-prose-body, #64748b)',
                lineHeight: 1.4,
                marginBottom: 0,
              }}
            >
              Forged by seasoned SREs, Operations specialists, and AI experts,<br />
              grounded in deep tech landscape experience and real-world operational insights.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
