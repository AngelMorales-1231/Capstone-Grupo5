"use client";
import { useParams } from "next/navigation";
import SubjectDetail from "@/components/SubjectDetail";

export default function DetalleEquipo() {
  const { id } = useParams<{ id: string }>();
  return <SubjectDetail tipo="equipos" id={id} />;
}
