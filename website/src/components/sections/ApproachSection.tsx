'use client';

import {
  IconLink,
  IconSparkles,
  IconTarget,
} from '@/components/icons/homepage';

export default function ApproachSection() {
  return (
    <section className="py-24 bg-slate-50 dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <h2 className="text-3xl font-bold text-center text-slate-900 dark:text-slate-50 mb-8">
          What Makes FaultMaven&apos;s Approach Different?
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-12 max-w-4xl mx-auto text-center">
          We&apos;re not just applying AI to old problems; we&apos;re fundamentally rethinking how engineers can overcome operational complexity. FaultMaven is built on a core belief: the most powerful solutions emerge when human expertise is actively augmented by specialized AI, in a partnership where you remain firmly in command.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-8 text-left">
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-white dark:bg-slate-800">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              Your AI Teammate: Intelligent Partnership, Not Impersonal Automation
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              Team up with FaultMaven, your expert AI Copilot. It rapidly sifts through complexity to surface critical insights and illuminate potential paths, <strong className="font-semibold text-slate-800 dark:text-slate-200">inspiring new perspectives and amplifying your expertise, not sidelining it.</strong> While it dramatically accelerates your workflow, <strong className="font-semibold text-slate-800 dark:text-slate-200">you direct the investigation and make all final calls.</strong> This human-centric control ensures an effective, <strong className="font-semibold text-slate-800 dark:text-slate-200">safe, and accountable</strong> approach to resolving complex challenges, harnessing AI&apos;s power while keeping your critical judgment firmly in charge.
            </p>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-white dark:bg-slate-800">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              Purpose-Built for Operational Complexity
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              This isn&apos;t a generic AI adapted for troubleshooting. FaultMaven is being developed from the ground up by seasoned engineers and domain experts who understand the unique pressures and data intricacies of modern operations. It&apos;s tailored to &quot;think&quot; like an experienced SRE, focusing on the signals and contexts that truly matter.
            </p>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-white dark:bg-slate-800">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              Clarity and Action, Not Just Data Lakes
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              Our focus isn&apos;t just on analyzing vast amounts of data, but on transforming that data into clear, understandable insights and concrete, actionable recommendations. FaultMaven aims to cut through the noise, providing the clarity you need to act decisively and effectively.
            </p>
          </div>
        </div>
        <p className="text-center text-lg text-slate-600 dark:text-slate-400 mt-12">
          This philosophy guides every stage of FaultMaven&apos;s development, from its initial 1.0 offering to its future as an integrated team expert. We&apos;re committed to building an AI that you can trust to make your toughest challenges more manageable.
        </p>
      </div>
    </section>
  );
}
