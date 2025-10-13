import dynamic from 'next/dynamic';
import ProblemSection from "@/components/sections/ProblemSection";
import ApproachSection from "@/components/sections/ApproachSection";
import CapabilitiesSection from "@/components/sections/CapabilitiesSection";
import GettingStartedSection from "@/components/sections/GettingStartedSection";
import VisionSnippet from "@/components/sections/VisionSnippet";

// Dynamically import Hero with loading fallback
const Hero = dynamic(() => import("@/components/sections/Hero"), {
  loading: () => (
    <div className="pt-32 pb-24 bg-slate-50 dark:bg-slate-900 animate-pulse">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid md:grid-cols-2 gap-16 items-center">
          <div className="space-y-6">
            <div className="h-12 bg-slate-200 dark:bg-slate-700 rounded w-3/4"></div>
            <div className="h-4 bg-slate-200 dark:bg-slate-700 rounded w-full"></div>
            <div className="h-4 bg-slate-200 dark:bg-slate-700 rounded w-5/6"></div>
            <div className="h-10 bg-slate-200 dark:bg-slate-700 rounded w-1/3"></div>
          </div>
          <div className="h-96 bg-slate-200 dark:bg-slate-700 rounded-xl"></div>
        </div>
      </div>
    </div>
  )
});

// Dynamically import FAQSnippet for code splitting
const FAQSnippet = dynamic(() => import("@/components/sections/FAQSnippet"), {
  loading: () => (
    <div className="py-16 bg-slate-50 dark:bg-slate-800/50 animate-pulse">
      <div className="max-w-4xl mx-auto px-6">
        <div className="h-8 bg-slate-200 dark:bg-slate-700 rounded w-1/2 mb-8"></div>
        <div className="grid md:grid-cols-2 gap-8">
          {[1, 2].map(i => (
            <div key={i} className="h-32 bg-slate-200 dark:bg-slate-700 rounded"></div>
          ))}
        </div>
      </div>
    </div>
  )
});

const FinalCTASection = dynamic(() => import('@/components/sections/FinalCTASection'), {
  loading: () => (
    <div className="py-20 bg-blue-50 dark:bg-blue-900/20 animate-pulse">
      <div className="max-w-4xl mx-auto px-6">
        <div className="h-12 bg-blue-200 dark:bg-blue-700 rounded w-1/2 mb-8"></div>
        <div className="h-8 bg-blue-200 dark:bg-blue-700 rounded w-1/3"></div>
      </div>
    </div>
  )
});

export default function Home() {
  return (
    <main>
      <Hero />
      <ProblemSection />
      <ApproachSection />
      <CapabilitiesSection />
      <GettingStartedSection />
      <VisionSnippet />
      <FAQSnippet />
      <FinalCTASection />
    </main>
  );
}
