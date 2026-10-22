from customer_registry_sync.assemble import assemble_dataset
from customer_registry_sync.schema import CustomerRecord, RawCustomer


def raw(name, bucket, **kw):
    return RawCustomer(name=name, bucket=bucket, **kw)


def test_cross_bucket_duplicate_collapses_to_one_record():
    recs = assemble_dataset(
        [
            raw("Hooli Casinos", "Customers", domains=["hoolicasinos.example.test"], arr=90000.0),
            raw("Hooli Casinos", "Prospects", domains=["hoolicasinos.example.test"]),
        ],
        self_managed=[],
        overrides={},
    )
    assert len(recs) == 1
    r = recs[0]
    assert set(r.buckets) == {"Customers", "Prospects"}
    assert r.classification == "real"  # Customers(real) beats Prospects(prospect)
    assert r.arr == 90000.0  # non-null value carried across the merge


def test_domain_first_keying_reconciles_name_aliases():
    recs = assemble_dataset(
        [
            raw("Bluth Family Services", "Churned Customers", domains=["bluthfamily.example.test"]),
            raw("BFSCO", "Prospects", domains=["bluthfamily.example.test"]),
        ],
        self_managed=[],
        overrides={},
    )
    assert len(recs) == 1
    r = recs[0]
    assert r.classification == "churned"  # churned beats prospect
    assert r.canonical_name == "Bluth Family Services"  # name from higher-rank bucket
    assert set(r.buckets) == {"Churned Customers", "Prospects"}


def test_name_keying_merges_when_no_domain():
    recs = assemble_dataset(
        [raw("Dunder Software", "Customers"), raw("dundersoftware", "Prospects")],
        self_managed=[],
        overrides={},
    )
    assert len(recs) == 1
    assert recs[0].classification == "real"


def test_deployment_taken_from_self_managed_list_else_null():
    recs = assemble_dataset(
        [
            raw("Stark Fabrication LLC (The Forge)", "Customers"),
            raw("Bluebird Media", "Customers", domains=["bluebirdmedia.example.test"]),
        ],
        self_managed=[
            CustomerRecord(
                canonical_name="Stark Fabrication LLC (The Forge)",
                deployment="on-prem",
                classification="real",
                sources=["self-managed-customers.yaml"],
            )
        ],
        overrides={},
    )
    by = {r.canonical_name: r for r in recs}
    assert by["Stark Fabrication LLC (The Forge)"].deployment == "on-prem"  # curated wins
    assert by["Bluebird Media"].deployment is None  # never invented


def test_self_managed_only_customer_is_added_with_its_classification():
    recs = assemble_dataset(
        [raw("Acme Robotics", "Customers", domains=["acmerobotics.example.test"])],
        self_managed=[
            CustomerRecord(
                canonical_name="Private Self-Managed Co",
                deployment="on-prem",
                classification="real",
                domains=["privateselfmanaged.example.test"],
                sources=["self-managed-customers.yaml"],
            )
        ],  # mirror load_self_managed_customers
        overrides={},
    )
    by = {r.canonical_name: r for r in recs}
    assert set(by) == {"Acme Robotics", "Private Self-Managed Co"}
    # self-managed-only customer (no Notion page) keeps its classification → not dropped
    assert by["Private Self-Managed Co"].classification == "real"
    assert by["Private Self-Managed Co"].deployment == "on-prem"


def test_classification_override_wins_over_bucket():
    recs = assemble_dataset(
        [raw("Demo Org", "Customers", domains=["demo.example.test"])],
        self_managed=[],
        overrides={"demo.example.test": {"classification": "internal", "reason": "demo"}},
    )
    assert recs[0].classification == "internal"


def test_override_by_name_also_applies():
    recs = assemble_dataset(
        [raw("Internal Test", "Customers")],
        self_managed=[],
        overrides={"Internal Test": {"classification": "internal"}},
    )
    assert recs[0].classification == "internal"
