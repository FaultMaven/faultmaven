import {
  Handshake,
  Lightbulb,
  Scale,
  Users,
  TrendingUp,
  CheckCircle2,
} from 'lucide-react';
import Link from 'next/link';

const guidingPrinciples = [
  {
    icon: TrendingUp,
    title: 'Value First, Always:',
    text: 'Your success truly defines ours. When we introduce pricing, it will directly reflect the significant, measurable benefits you gain from your AI Copilot: substantially reduced MTTR, critical time saved for your engineering teams, enhanced problem-solving capabilities, and the power of easily accessible, collaborative knowledge. We aim for FaultMaven to be an investment that pays for itself many times over.',
  },
  {
    icon: Lightbulb,
    title: 'Fueling Sustainable Innovation:',
    text: 'Fair and thoughtful pricing ensures we can continuously invest in the cutting-edge research, development, and features that FaultMaven needs to evolve and consistently meet your most demanding operational challenges. This sustainability is key to our long-term partnership with you.',
  },
  {
    icon: Scale,
    title: 'Transparency & Scalable Fairness:',
    text: 'Expect no convoluted schemes or hidden costs. Our future pricing will be clear, straightforward, and designed to scale intuitively with your needs—from empowering individual engineers with FaultMaven 1.0 to supporting the collaborative intelligence of entire teams with our 2.0 vision.',
  },
  {
    icon: Handshake,
    title: 'Partnership at Our Core:',
    text: "Your insights, especially as an early partner, are invaluable. We are genuinely building FaultMaven with you, not just for you. This collaborative spirit means we listen intently and adapt, ensuring the product and its value grow in alignment with your real-world experiences.",
  },
  {
    icon: Users,
    title: 'Commitment to the Community:',
    text: 'We believe in the power of the engineering community and the strategic value of giving back. As FaultMaven grows, we are committed to supporting the AIOps and SRE ecosystems, actively exploring opportunities for open-source contributions and the sharing of knowledge and resources.',
  },
];

const earlyPartnerBenefits = [
  'Shape the tool solving your toughest operational challenges.',
  'Gain early expertise with a groundbreaking AI Copilot.',
  'Earn founding member status in our pioneering FaultMaven community.',
];

export default function PricingPage() {
  return (
    <main>
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
            Fair Pricing, Built Together: Our Commitment to Value
          </h1>
          <p className="mt-6 text-lg max-w-2xl mx-auto text-slate-600 dark:text-slate-400">
            At FaultMaven, our very existence is dedicated to delivering exceptional value to you, our fellow engineers and operations professionals. While we&apos;re currently focused on perfecting FaultMaven 1.0 in close collaboration with our early design partners, we believe it&apos;s important to share our philosophy on pricing—an approach grounded in fairness, transparency, and mutual success. We&apos;re taking the time to get this right, just as we are with the product itself.
          </p>
        </div>
      </section>

      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-800 dark:text-slate-200 mb-4 text-center">
            Our Guiding Principles on Value & Future Pricing:
          </h2>
          <p className="text-center text-slate-600 dark:text-slate-400 mb-12">
            We believe in building FaultMaven on a foundation of trust, collaboration, and mutual success. Here&apos;s what will shape our approach:
          </p>
          <div className="space-y-8">
            {guidingPrinciples.map((principle, index) => (
              <div key={index} className="flex items-start gap-6 p-6 border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-900 shadow-sm">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/50 flex items-center justify-center">
                  <principle.icon className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-2">{principle.title}</h3>
                  <p className="text-slate-600 dark:text-slate-400">{principle.text}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-3xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-800 dark:text-slate-200 mb-4 text-center">
            The Value of Early Partnership
          </h2>
          <p className="text-center text-slate-600 dark:text-slate-400 mb-8">
            Joining us during these foundational stages offers a unique opportunity beyond just early access:
          </p>
          <div className="space-y-4 max-w-2xl mx-auto">
            {earlyPartnerBenefits.map((benefit, index) => (
              <div key={index} className="flex items-start gap-4">
                <CheckCircle2 className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" />
                <p className="text-lg text-slate-700 dark:text-slate-300">
                  {benefit}
                </p>
              </div>
            ))}
          </div>
           <p className="text-center text-slate-600 dark:text-slate-400 mt-8">
            We believe in building lasting relationships, and the contributions of our early partners are immensely valuable to us.
          </p>
        </div>
      </section>

      <section className="bg-white dark:bg-slate-900">
        <div className="max-w-3xl mx-auto py-16 px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-4">
            Stay Informed
          </h2>
          <p className="text-lg text-slate-600 dark:text-slate-400 mb-8 max-w-2xl mx-auto">
            We invite you to join us as we build. Be the first to receive updates on FaultMaven&apos;s development, early access programs, and our approach to pricing as it solidifies.
          </p>
          <Link
            href="/waitlist"
            className="inline-flex items-center justify-center px-8 py-4 text-lg font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 transition duration-200"
          >
            Join the Waitlist & Get Key Updates
          </Link>
        </div>
      </section>
    </main>
  );
}
