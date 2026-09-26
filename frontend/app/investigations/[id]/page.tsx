import { InvestigationDetail } from "@/components/InvestigationDetail";

export default async function InvestigationPage({ params }: PageProps<"/investigations/[id]">) {
  const { id } = await params;
  return <InvestigationDetail id={id} />;
}
