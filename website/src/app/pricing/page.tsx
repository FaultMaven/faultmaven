import Link from 'next/link';
import Button from '@/components/ui/Button';

const guidingPrinciples = [
  {
    title: 'Value First, Always:',
    text: 'Your success truly defines ours. When we introduce pricing, it will directly reflect the significant, measurable benefits you gain from your AI Copilot: substantially reduced MTTR, critical time saved for your engineering teams, enhanced problem-solving capabilities, and the power of easily accessible, collaborative knowledge. We aim for FaultMaven to be an investment that pays for itself many times over.'
  },
  {
    title: 'Fueling Sustainable Innovation:',
    text: 'Fair and thoughtful pricing ensures we can continuously invest in the cutting-edge research, development, and features that FaultMaven needs to evolve and consistently meet your most demanding operational challenges. This sustainability is key to our long-term partnership with you.'
  },
  {
    title: 'Transparency & Scalable Fairness:',
    text: 'Expect no convoluted schemes or hidden costs. Our future pricing will be clear, straightforward, and designed to scale intuitively with your needs—from empowering individual engineers with FaultMaven 1.0 to supporting the collaborative intelligence of entire teams with our 2.0 vision.'
  },
  {
    title: 'Partnership at Our Core:',
    text: 'Your insights, especially as an early partner, are invaluable. We are genuinely building FaultMaven with you, not just for you. This collaborative spirit means we listen intently and adapt, ensuring the product and its value grow in alignment with your real-world experiences.'
  },
  {
    title: 'Commitment to the Community:',
    text: 'We believe in the power of the engineering community and the strategic value of giving back. As FaultMaven grows, we are committed to supporting the AIOps and SRE ecosystems, actively exploring opportunities for open-source contributions and the sharing of knowledge and resources.'
  }
];

const earlyPartnerBenefits = [
  {
    title: 'Shape the Tool:',
    text: 'Directly influence the development of a solution designed to solve your toughest operational challenges.'
  },
  {
    title: 'Gain Early Expertise:',
    text: 'Become proficient with a groundbreaking AI Copilot and lead the way in AI-augmented troubleshooting.'
  },
  {
    title: 'Earn Founding Member Status:',
    text: 'Be recognized as an integral part of the pioneering FaultMaven community, with a voice in its evolution.'
  }
];

export default function PricingPage() {
  return (
    <main className="min-h-screen bg-white dark:bg-slate-900">
      {/* Hero Section */}
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
            Fair Pricing, Built Together: Our Commitment to Value
          </h1>
          <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto">
            At FaultMaven, our very existence is dedicated to delivering exceptional value to you, our fellow engineers and operations professionals. While we&apos;re currently focused on perfecting FaultMaven 1.0 in close collaboration with our early design partners, we believe it&apos;s important to share our philosophy on pricing—an approach grounded in fairness, transparency, and mutual success. We&apos;re taking the time to get this right, just as we are with the product itself.
          </p>
        </div>
      </section>

      {/* Guiding Principles Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-10 text-center md:text-left">
            Our Guiding Principles on Value & Future Pricing:
          </h2>
          <div className="space-y-8">
            {guidingPrinciples.map((principle, index) => (
              <div key={index} className="p-6 border border-slate-200 dark:border-slate-700 rounded-xl bg-white dark:bg-slate-900 shadow-sm">
                <h3 className="text-xl font-semibold text-slate-900 dark:text-slate-100 mb-3">{principle.title}</h3>
                <p className="text-slate-700 dark:text-slate-300">{principle.text}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Early Partner Benefits Section */}
      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-50 mb-8 text-center md:text-left">
            Why Partner with FaultMaven Early?
          </h2>
          <div className="prose prose-lg max-w-none text-slate-700 dark:text-slate-300 space-y-4 mb-8">
            <p>
              Joining us during these foundational stages offers a unique opportunity beyond just early access:
            </p>
            <ul className="list-disc pl-5 space-y-2">
              {earlyPartnerBenefits.map((benefit, index) => (
                <li key={index}><strong>{benefit.title}</strong> {benefit.text}</li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section className="py-16 px-6 bg-slate-100 dark:bg-slate-800">
        <div className="max-w-3xl mx-auto text-center rounded-xl p-10 bg-white dark:bg-slate-900 shadow-md">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6">
            Stay Informed & Join the Journey:
          </h2>
          <div className="prose prose-lg max-w-none text-slate-700 dark:text-slate-300 space-y-6 mx-auto">
            <p>
              We invite you to join us as we build. Be the first to receive updates on FaultMaven&apos;s development, early access programs, and our approach to pricing as it solidifies.
            </p>
            <Button asChild href="/waitlist" variant="primary" className="mt-4">
              Join the Waitlist for Updates & Early Access Opportunities
            </Button>
          </div>
        </div>
      </section>
    </main>
  );
}
