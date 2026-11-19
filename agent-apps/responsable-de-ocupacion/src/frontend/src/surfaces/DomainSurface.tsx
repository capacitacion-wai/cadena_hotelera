import { ComponentType, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useManifest } from '../hooks/useManifest';
import DomainList from '../pages/DomainList';
import InboxSurface from './InboxSurface';
import LedgerSurface from './LedgerSurface';
import OutboxSurface from './OutboxSurface';
import AdvisorySurface from './AdvisorySurface';
import BoardSurface from './BoardSurface';
import { resolveArchetype, Archetype } from './resolveArchetype';

/**
 * Router for "Lo que genero": resolves an entity's business SURFACE (archetype)
 * and renders it. Registered surfaces below take over; every archetype WITHOUT
 * a registered component — and `register` itself — falls back to the tabular
 * `DomainList`. That fallback is deliberate: it keeps every intermediate build
 * compiling and behaving exactly like the previous CRUD grid until each purpose-
 * built surface lands, and it is the permanent home for genuinely tabular
 * reference data (the last-resort archetype).
 */
const SURFACES: Partial<Record<Archetype, ComponentType>> = {
  inbox: InboxSurface,
  outbox: OutboxSurface,
  advisory: AdvisorySurface,
  ledger: LedgerSurface,
  board: BoardSurface,
  // register → DomainList (the tabular fallback, handled below).
};

export default function DomainSurface() {
  const { slug } = useParams<{ slug: string }>();
  const { manifest } = useManifest();
  const entity = useMemo(() => {
    const list = (manifest as any)?.domain_entities ?? [];
    return list.find((e: any) => e.slug === slug);
  }, [manifest, slug]);

  const archetype: Archetype = entity ? resolveArchetype(entity) : 'register';
  const Surface = SURFACES[archetype];
  return Surface ? <Surface /> : <DomainList />;
}
