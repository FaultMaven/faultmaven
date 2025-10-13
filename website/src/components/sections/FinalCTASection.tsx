'use client';

import Button from '../ui/Button';

export default function FinalCTASection() {
  return (
    <section className="py-24 bg-slate-100 dark:bg-slate-800">
      <div className="max-w-6xl mx-auto px-6">
        <h2 className="text-3xl font-bold text-center text-slate-900 dark:text-slate-50 mb-8">
          Shape the Future of AIOps with FaultMaven
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-4 max-w-4xl mx-auto text-center">
          FaultMaven is at a pivotal stage of development, and we&apos;re building it for—and with—forward-thinking engineers and organizations. While some details remain under wraps as we refine our core technology, this is your invitation to get involved early with a select group and help shape a truly transformative solution.
        </p>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-12 max-w-4xl mx-auto text-center">
          Here&apos;s how you can participate:
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
          <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg shadow-sm bg-white dark:bg-slate-800/50 flex flex-col">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              For Experienced SREs & Operations Engineers
            </h3>
            <p className="text-slate-600 dark:text-slate-400 mb-6 flex-grow">
              Do you want an early look at FaultMaven 1.0 and a chance to provide crucial feedback that directly shapes its features? We&apos;re looking for design partners to share their toughest troubleshooting scenarios and help us build the ultimate AI Copilot.
            </p>
            <Button asChild href="/waitlist" variant="primary" className="text-center">
              Apply for Our Private Preview & Design Partner Program
            </Button>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg shadow-sm bg-white dark:bg-slate-800/50 flex flex-col">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              For Visionary Investors & Strategic Partners
            </h3>
            <p className="text-slate-600 dark:text-slate-400 mb-6 flex-grow">
              We believe FaultMaven offers a unique approach to a significant market need, backed by seasoned expertise and a clear, phased roadmap. If you share our vision and are interested in growth opportunities:
            </p>
            <Button asChild href="/contact" variant="secondary" className="text-center">
              Request Our Vision Deck & Discuss Partnership
            </Button>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg shadow-sm bg-white dark:bg-slate-800/50 flex flex-col">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              For Passionate Innovators & Talent
            </h3>
            <p className="text-slate-600 dark:text-slate-400 mb-6 flex-grow">
              Excited by the mission to redefine operational troubleshooting with AI? We&apos;ll be looking for exceptional individuals to join our core team as we grow.
            </p>
            <Button asChild href="/contact" variant="secondary" className="text-center">
              Express Interest in Future Roles & Collaborations
            </Button>
          </div>
        </div>
        <p className="text-center text-slate-600 dark:text-slate-400 mt-12">
          We&apos;re building something we believe is truly special, and we&apos;re eager to connect with those who want to be part of the journey from these early stages.
        </p>
      </div>
    </section>
  );
}
