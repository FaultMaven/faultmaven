'use client';

import Button from '../ui/Button';

export default function VisionSnippet() {
  return (
    <section className="py-24 bg-slate-100 dark:bg-slate-800">
      <div className="max-w-4xl mx-auto px-6 text-center">
        <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-50 mb-4">
          Your AI Copilot Today, Your Team&apos;s Collective Brain Tomorrow
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-8">
          FaultMaven 1.0 delivers powerful personal AI assistance right now. Looking ahead, we&apos;re developing FaultMaven into an integrated AI solution that transforms collaborative problem-solving and amplifies your entire team&apos;s effectiveness by fostering a shared, intelligent understanding. We&apos;re releasing details in phases as this vision matures, and <strong className="text-slate-700 dark:text-slate-300">we invite forward-thinking engineers and organizations to help shape this journey with us.</strong>
        </p>
        <Button asChild href="/roadmap" variant="secondary">
          Explore Our Full Vision & Learn How to Participate
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-5 h-5 ml-2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
          </svg>
        </Button>
      </div>
    </section>
  );
}
