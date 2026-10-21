export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="font-medium text-cc-navy">{title}</h2>
      <div className="mt-2 text-sm">{children}</div>
    </section>
  );
}
