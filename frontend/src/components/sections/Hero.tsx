'use client';

import Image from 'next/image';
import heroImage from '../../../public/images/hero-mttr.webp';

export default function Hero() {
  return (
    <section className="pt-24 pb-16 bg-slate-50 dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid md:grid-cols-2 gap-12 items-center">
          {/* Left Column: Text Content */}
          <div className="text-left">
            <h1 className="text-4xl md:text-5xl font-bold leading-tight mb-4 text-slate-900 dark:text-slate-50">
              Troubleshoot Faster, Smarter, with <span className="text-blue-600">FaultMaven</span>
            </h1>
            <p className="text-xl text-slate-600 dark:text-slate-400 mb-8 max-w-2xl">
              Your evolving AI Copilot—built <span className="italic">with</span> engineers like you—delivering real-time insights and guided solutions to dramatically reduce Mean Time To Resolution (MTTR) across your toughest operational challenges.
            </p>
            <a
              href="/waitlist"
              className="inline-flex items-center justify-center px-6 py-3 text-base font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 transition duration-200"
            >
              Get Early Access Updates & Help Shape FaultMaven
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-5 h-5 ml-2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
              </svg>
            </a>
          </div>

          {/* Right Column: Image */}
          <div>
            <Image
              src={heroImage}
              alt="FaultMaven AI Copilot in action, analyzing code to reduce MTTR"
              priority
              placeholder="blur"
              className="rounded-lg shadow-xl"
            />
          </div>
        </div>

        {/* Credibility Statement Below */}
        <div className="text-center mt-20">
          <hr className="my-8 border-slate-200 dark:border-slate-700" />
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Built on deep tech landscape experience and real-world operational insights.
          </p>
          <p className="text-sm font-medium text-slate-600 dark:text-slate-300 mt-1">
            Forged by seasoned SREs, Operations specialists, and AI experts.
          </p>
        </div>
      </div>
    </section>
  );
}
