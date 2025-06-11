'use client';

import Image from 'next/image';
import Button from '../ui/Button';
import heroImage from '../../../public/images/hero-mttr.webp';

export default function Hero() {
  return (
    <section className="pt-32 pb-24 bg-slate-50 dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid md:grid-cols-2 gap-16 items-center">
          {/* Left Column: Text Content */}
          <div className="text-left">
            <h1 className="text-4xl md:text-5xl font-bold leading-tight mb-6 text-slate-900 dark:text-slate-50">
              Troubleshoot Faster, Smarter, with <span className="text-blue-600">FaultMaven</span>
            </h1>
            <p className="text-xl text-slate-600 dark:text-slate-400 mb-10 max-w-2xl">
              Your evolving AI Copilot—built <span className="italic">with</span> engineers like you—delivering real-time insights and guided solutions to dramatically reduce Mean Time To Resolution (MTTR) across your toughest operational challenges.
            </p>
            <Button asChild href="/waitlist" variant="primary" className="max-w-2xl">
              Get Early Access Updates & Help Shape FaultMaven
            </Button>
          </div>

          {/* Right Column: Image */}
          <div className="relative">
            <Image
              src={heroImage}
              alt="FaultMaven AI Copilot in action, analyzing code to reduce MTTR"
              priority
              placeholder="blur"
              className="rounded-xl shadow-2xl"
            />
          </div>
        </div>

        {/* Credibility Statement Below */}
        <div className="text-center mt-24">
          <hr className="my-10 border-slate-200 dark:border-slate-700" />
          <p className="text-base text-slate-500 dark:text-slate-400">
            Built on deep tech landscape experience and real-world operational insights.
          </p>
          <p className="text-base font-medium text-slate-600 dark:text-slate-300 mt-2">
            Forged by seasoned SREs, Operations specialists, and AI experts.
          </p>
        </div>
      </div>
    </section>
  );
}
